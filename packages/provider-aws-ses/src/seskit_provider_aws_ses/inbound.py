"""Creating the AWS plumbing that brings received mail in.

SES can receive mail for a verified domain, but it does nothing with it until
told to. Getting a message from the sender's server to SESKit takes six
resources across four services::

    S3 bucket          seskit-...-inbound   holds the raw message
    bucket policy      SES -> bucket        without it SES may not write
    SNS topic          seskit-...-inbound   announces that mail arrived
    SQS queue          seskit-...-inbound   the worker polls this
    queue policy       topic -> queue       without it SNS may not deliver
    receipt rule       domain -> S3 + SNS   one per domain, in the active rule set

The announcement carries the message's metadata and the key it was stored
under, never its content. SES offers to put the whole message in the SNS
notification, and that route is capped at 150 KB; the S3 route goes to 40 MB,
which is the difference between mail that works and mail that works until
somebody attaches a photograph.

**Never SES's own message encryption.** It encrypts on the client side with a
KMS key, and the docs name only the Java and Ruby SDKs as able to decrypt it -
there is nothing to read the message back with in Python. Encryption is the
bucket's, which S3 removes transparently on read.

**Never replace the active rule set.** An account has one, and the mail flowing
through it is somebody's. :meth:`SESInboundProvisioner.add_inbound_rule` adds to
it. A rule with no ``After`` goes to the front, and this one has no Stop action,
so it neither hides behind another rule nor changes what any of them does - a
message that matches both simply gets both.

**Everything here is idempotent**, for the same reason the events provisioner
is: a user who presses the button twice because nothing appeared to happen gets
one set of resources.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.providers.types import AWSCredentials, InboundInfrastructure, InboundRule

from seskit_provider_aws_ses.client import BOTO_CONFIG, build_session, call, polling_config
from seskit_provider_aws_ses.errors import error_code, normalise_boto_error
from seskit_provider_aws_ses.provisioning import _QueueTopicPlumbing

logger = get_logger(__name__)

#: Every rule SESKit adds starts with this, and the bucket policy admits only
#: rules that do. Without the condition any receipt rule in the account could
#: write into the bucket, including one somebody else created.
RULE_NAME_PREFIX = "seskit-"

#: The rule set created when the account has no active one. Named, so that a
#: later run recognises it as SESKit's own.
DEFAULT_RULE_SET = "seskit-inbound"

#: What SES documents as the ceiling on a stored message, headers included.
#: Anything larger than this coming out of the bucket was not put there by SES.
MAX_MESSAGE_BYTES = 40 * 1024 * 1024

#: Left to finish an incomplete multipart upload before S3 discards its parts.
#: SES does not use multipart, so this only tidies a failed write.
_ABORT_UPLOAD_DAYS = 1

S3_CREATE_ACTION = "s3:CreateBucket"
S3_PUBLIC_ACCESS_ACTION = "s3:PutBucketPublicAccessBlock"
S3_ENCRYPTION_ACTION = "s3:PutEncryptionConfiguration"
S3_LIFECYCLE_ACTION = "s3:PutLifecycleConfiguration"
S3_POLICY_ACTION = "s3:PutBucketPolicy"
S3_DELETE_ACTION = "s3:DeleteBucket"
S3_READ_ACTION = "s3:GetObject"
STS_IDENTITY_ACTION = "sts:GetCallerIdentity"
SES_DESCRIBE_ACTIVE_ACTION = "ses:DescribeActiveReceiptRuleSet"
SES_CREATE_RULE_SET_ACTION = "ses:CreateReceiptRuleSet"
SES_SET_ACTIVE_ACTION = "ses:SetActiveReceiptRuleSet"
SES_CREATE_RULE_ACTION = "ses:CreateReceiptRule"
SES_UPDATE_RULE_ACTION = "ses:UpdateReceiptRule"
SES_DELETE_RULE_ACTION = "ses:DeleteReceiptRule"
SES_DESCRIBE_RULE_SET_ACTION = "ses:DescribeReceiptRuleSet"
SES_DELETE_RULE_SET_ACTION = "ses:DeleteReceiptRuleSet"

_ALREADY_EXISTS = frozenset({"AlreadyExists"})
_BUCKET_OWNED = frozenset({"BucketAlreadyOwnedByYou"})
_BUCKET_TAKEN = frozenset({"BucketAlreadyExists"})
_BUCKET_NOT_EMPTY = frozenset({"BucketNotEmpty"})
_MISSING = frozenset({"RuleDoesNotExist", "RuleSetDoesNotExist", "NoSuchBucket"})
_NO_SUCH_OBJECT = frozenset({"NoSuchKey"})

EXPIRED_MESSAGE = (
    "That message is no longer in storage. Received mail is kept for the "
    "retention period and then removed."
)


def bucket_policy(*, bucket: str, region: str, account_id: str) -> str:
    """Let SES write to this bucket, from this account's SESKit rules, and only
    over TLS.

    Scoped twice: by account, so another account's rule cannot write here, and
    by the rule-name prefix, so neither can one of this account's own that
    SESKit did not make. The rule-set segment is a wildcard because the set a
    rule lands in is whichever one the account has active.
    """
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "seskit-allow-ses-puts",
                    "Effect": "Allow",
                    "Principal": {"Service": "ses.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                    "Condition": {
                        "StringEquals": {"AWS:SourceAccount": account_id},
                        "ArnLike": {
                            "AWS:SourceArn": (
                                f"arn:aws:ses:{region}:{account_id}:"
                                f"receipt-rule-set/*:receipt-rule/{RULE_NAME_PREFIX}*"
                            )
                        },
                    },
                },
                {
                    "Sid": "seskit-tls-only",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


def receipt_rule(*, name: str, domain: str, bucket: str, topic_arn: str) -> dict[str, Any]:
    """The rule for one domain: store the message, then announce it.

    ``ObjectKeyPrefix`` is the rule's own name, so one domain's mail sits apart
    from another's and the key says where it came from. Scanning stays on -
    SES records the spam, virus, SPF, DKIM and DMARC verdicts and takes no
    action on them, which leaves the decision to the person reading the mail.
    """
    return {
        "Name": name,
        "Enabled": True,
        "TlsPolicy": "Optional",
        "Recipients": [domain],
        "ScanEnabled": True,
        "Actions": [
            {
                "S3Action": {
                    "BucketName": bucket,
                    "ObjectKeyPrefix": f"{name}/",
                    "TopicArn": topic_arn,
                }
            }
        ],
    }


class SESInboundProvisioner(_QueueTopicPlumbing):
    """Builds and removes the receiving plumbing in one account and region.

    Constructed per request, like the other provisioners, and for the same
    reasons: the region varies per project and a cached client outlives the
    credentials it was built with.
    """

    # ------------------------------------------------------------- create ---

    async def provision_inbound(
        self,
        *,
        bucket_name: str,
        topic_name: str,
        queue_name: str,
        retention_days: int,
    ) -> InboundInfrastructure:
        """Create the bucket, topic and queue, in the order the dependencies
        require.

        The bucket is locked down before anything can write to it, and the
        queue policy exists before the subscription - a topic subscribed to a
        queue it may not write to drops messages without erroring.
        """
        if retention_days < 1:
            raise APIError(ErrorType.INVALID_REQUEST, "Retention must be at least one day.")

        account_id = await self._account_id()
        await self._create_bucket(bucket_name)
        await self._secure_bucket(bucket_name, account_id=account_id, retention_days=retention_days)

        topic_arn = await self._create_topic(topic_name)
        queue_url = await self._create_queue(queue_name)
        queue_arn = await self._queue_arn(queue_url)
        await self._allow_topic_to_send(
            queue_url=queue_url, queue_arn=queue_arn, topic_arn=topic_arn
        )
        subscription_arn = await self._subscribe(topic_arn, protocol="sqs", endpoint=queue_arn)

        logger.info("inbound_infrastructure_provisioned", region=self.region)
        return InboundInfrastructure(
            bucket=bucket_name,
            topic_arn=topic_arn,
            queue_url=queue_url,
            queue_arn=queue_arn,
            subscription_arn=subscription_arn,
        )

    async def add_inbound_rule(
        self, infrastructure: InboundInfrastructure, *, domain: str, rule_name: str
    ) -> InboundRule:
        """Receive mail for one domain, without disturbing anything else.

        The rule goes into the active rule set. Only when the account has none
        is one created - and activated last, after the rule is in it, so there
        is never a moment when the account is receiving through an empty set.
        """
        if not rule_name.startswith(RULE_NAME_PREFIX):
            # The bucket policy would refuse this rule's writes, and the mail
            # would be accepted by SES and then vanish. Failing here is the
            # only place that failure is visible.
            raise APIError(
                ErrorType.INVALID_REQUEST,
                f"Receipt rule names must start with {RULE_NAME_PREFIX!r}.",
            )

        rule = receipt_rule(
            name=rule_name,
            domain=domain,
            bucket=infrastructure.bucket,
            topic_arn=infrastructure.topic_arn,
        )
        active = await self._active_rule_set()

        if active is None:
            await self._create_rule_set(DEFAULT_RULE_SET)
            await self._put_rule(DEFAULT_RULE_SET, rule, existing=[])
            await self._activate(DEFAULT_RULE_SET)
            logger.info("inbound_rule_set_created", region=self.region)
            return InboundRule(name=rule_name, rule_set=DEFAULT_RULE_SET, created_rule_set=True)

        rule_set = str(active["Metadata"]["Name"])
        await self._put_rule(
            rule_set, rule, existing=[str(r["Name"]) for r in active.get("Rules", [])]
        )
        logger.info("inbound_rule_added", region=self.region)
        return InboundRule(
            name=rule_name,
            rule_set=rule_set,
            # A set SESKit made on an earlier run is still SESKit's.
            created_rule_set=rule_set == DEFAULT_RULE_SET,
        )

    # ------------------------------------------------------------- remove ---

    async def remove_inbound_rule(self, rule: InboundRule) -> None:
        """Remove one domain's rule, and the rule set only if it was ours and
        is now empty.

        A set the user made is theirs however empty it ends up, and an empty
        set SESKit made is not worth leaving behind. Deactivating first is
        required - SES will not delete the active set - and is skipped unless
        this set is the active one.
        """
        await self._tolerate_missing(
            self._receipt().delete_receipt_rule,
            action=SES_DELETE_RULE_ACTION,
            RuleSetName=rule.rule_set,
            RuleName=rule.name,
        )

        if not rule.created_rule_set:
            return

        try:
            described = await call(
                self._receipt().describe_receipt_rule_set, RuleSetName=rule.rule_set
            )
        except ClientError as exc:
            if error_code(exc) not in _MISSING:
                logger.warning(
                    "inbound_teardown_step_failed",
                    action=SES_DESCRIBE_RULE_SET_ACTION,
                    code=error_code(exc),
                )
            return
        if described.get("Rules"):
            return

        active = await self._active_rule_set()
        if active is not None and active["Metadata"]["Name"] == rule.rule_set:
            await self._tolerate_missing(
                self._receipt().set_active_receipt_rule_set, action=SES_SET_ACTIVE_ACTION
            )
        await self._tolerate_missing(
            self._receipt().delete_receipt_rule_set,
            action=SES_DELETE_RULE_SET_ACTION,
            RuleSetName=rule.rule_set,
        )

    async def remove_inbound(self, infrastructure: InboundInfrastructure) -> bool:
        """Remove the topic, queue and - if it is empty - the bucket.

        The subscription goes before the queue, or SNS spends a while retrying
        into one that no longer exists. Each step tolerates "already gone" and
        none stops the others, as in the events teardown.

        A bucket that still holds mail stays. The bucket's expiry empties it,
        and a teardown that deleted received messages as a side effect of
        switching receiving off would be doing something nobody asked for.
        """
        if infrastructure.subscription_arn:
            await self._unsubscribe(infrastructure.subscription_arn)
        if infrastructure.topic_arn:
            await self._delete_topic(infrastructure.topic_arn)
        if infrastructure.queue_url:
            await self._delete_queue(infrastructure.queue_url)

        removed = True
        if infrastructure.bucket:
            removed = await self._delete_bucket(infrastructure.bucket)

        logger.info("inbound_infrastructure_removed", region=self.region, bucket_removed=removed)
        return removed

    # ------------------------------------------------------------ internal ---

    def _s3(self) -> Any:
        return self._session.client("s3", config=BOTO_CONFIG)

    def _receipt(self) -> Any:
        """The classic SES client. Receipt rules live there, not in SES v2."""
        return self._session.client("ses", config=BOTO_CONFIG)

    async def _account_id(self) -> str:
        try:
            response = await call(
                self._session.client("sts", config=BOTO_CONFIG).get_caller_identity
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=STS_IDENTITY_ACTION) from exc
        return str(response["Account"])

    async def _create_bucket(self, name: str) -> None:
        arguments: dict[str, Any] = {"Bucket": name}
        if self.region != "us-east-1":
            # us-east-1 is the one region that refuses a location constraint.
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        try:
            await call(self._s3().create_bucket, **arguments)
        except ClientError as exc:
            code = error_code(exc)
            if code in _BUCKET_OWNED:
                return
            if code in _BUCKET_TAKEN:
                raise APIError(
                    ErrorType.PROVIDER_ERROR,
                    "The S3 bucket name SESKit chose is already in use by another "
                    "AWS account. Bucket names are global across all of AWS.",
                ) from exc
            raise normalise_boto_error(exc, action=S3_CREATE_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=S3_CREATE_ACTION) from exc

    async def _secure_bucket(self, bucket: str, *, account_id: str, retention_days: int) -> None:
        """Private, encrypted, expiring, and writable by SESKit's rules only."""
        client = self._s3()
        steps: list[tuple[str, Any, dict[str, Any]]] = [
            (
                S3_PUBLIC_ACCESS_ACTION,
                client.put_public_access_block,
                {
                    "Bucket": bucket,
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                },
            ),
            (
                S3_ENCRYPTION_ACTION,
                client.put_bucket_encryption,
                {
                    "Bucket": bucket,
                    "ServerSideEncryptionConfiguration": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                },
            ),
            (
                S3_LIFECYCLE_ACTION,
                client.put_bucket_lifecycle_configuration,
                {
                    "Bucket": bucket,
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "ID": "seskit-expire-received-mail",
                                "Status": "Enabled",
                                "Filter": {"Prefix": ""},
                                "Expiration": {"Days": retention_days},
                                "AbortIncompleteMultipartUpload": {
                                    "DaysAfterInitiation": _ABORT_UPLOAD_DAYS
                                },
                            }
                        ]
                    },
                },
            ),
            (
                S3_POLICY_ACTION,
                client.put_bucket_policy,
                {
                    "Bucket": bucket,
                    "Policy": bucket_policy(
                        bucket=bucket, region=self.region, account_id=account_id
                    ),
                },
            ),
        ]
        for action, func, arguments in steps:
            try:
                await call(func, **arguments)
            except Exception as exc:
                raise normalise_boto_error(exc, action=action) from exc

    async def _delete_bucket(self, bucket: str) -> bool:
        """Whether the bucket is gone. False means it was kept, with mail in it."""
        try:
            await call(self._s3().delete_bucket, Bucket=bucket)
        except ClientError as exc:
            code = error_code(exc)
            if code in _MISSING:
                return True
            if code in _BUCKET_NOT_EMPTY:
                logger.info("inbound_bucket_kept", reason="not_empty")
                return False
            logger.warning("inbound_teardown_step_failed", action=S3_DELETE_ACTION, code=code)
            return False
        except Exception:
            logger.warning("inbound_teardown_step_failed", action=S3_DELETE_ACTION)
            return False
        return True

    async def _active_rule_set(self) -> dict[str, Any] | None:
        """The account's active rule set with its rules, or ``None``."""
        try:
            response = await call(self._receipt().describe_active_receipt_rule_set)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_DESCRIBE_ACTIVE_ACTION) from exc
        if not (response.get("Metadata") or {}).get("Name"):
            return None
        return dict(response)

    async def _create_rule_set(self, name: str) -> None:
        try:
            await call(self._receipt().create_receipt_rule_set, RuleSetName=name)
        except ClientError as exc:
            if error_code(exc) in _ALREADY_EXISTS:
                return
            raise normalise_boto_error(exc, action=SES_CREATE_RULE_SET_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_CREATE_RULE_SET_ACTION) from exc

    async def _put_rule(self, rule_set: str, rule: dict[str, Any], *, existing: list[str]) -> None:
        """Create the rule, or update it if it is already there, so that the
        caller ends up with the rule in the state it asked for.
        """
        client = self._receipt()
        creating = rule["Name"] not in existing
        func = client.create_receipt_rule if creating else client.update_receipt_rule
        action = SES_CREATE_RULE_ACTION if creating else SES_UPDATE_RULE_ACTION
        try:
            await call(func, RuleSetName=rule_set, Rule=rule)
        except Exception as exc:
            raise normalise_boto_error(exc, action=action) from exc

    async def _activate(self, rule_set: str) -> None:
        try:
            await call(self._receipt().set_active_receipt_rule_set, RuleSetName=rule_set)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_SET_ACTIVE_ACTION) from exc

    async def _tolerate_missing(self, func: Any, *, action: str, **kwargs: Any) -> None:
        """As the base class does, but also for a rule or rule set already gone."""
        try:
            await call(func, **kwargs)
        except ClientError as exc:
            code = error_code(exc)
            if code in _MISSING:
                return
            logger.warning("inbound_teardown_step_failed", action=action, code=code)
        except Exception:
            logger.warning("inbound_teardown_step_failed", action=action)


class S3InboundStore:
    """Reads a received message back out of its bucket.

    Credentials come from the connection that owns the bucket, exactly as they
    do for the queue reader.
    """

    def __init__(self, region: str, credentials: AWSCredentials) -> None:
        self.region = region
        self._session = build_session(region, credentials)

    async def fetch_message(self, *, bucket: str, key: str) -> bytes:
        """The raw, unmodified message.

        A message that has expired is a different fact from one that could not
        be read, and callers need to tell them apart: the first is settled and
        will never come back, the second may well succeed on a retry.
        """
        # A longer read timeout than the request path's: this is a download of
        # up to 40 MB, not a call that should fail in fifteen seconds.
        client = self._session.client("s3", config=polling_config(30))
        try:
            response = await call(client.get_object, Bucket=bucket, Key=key)
            if int(response.get("ContentLength", 0)) > MAX_MESSAGE_BYTES:
                raise APIError(
                    ErrorType.PROVIDER_ERROR,
                    "That object is larger than any message SES stores.",
                )
            body = await call(response["Body"].read)
        except APIError:
            raise
        except ClientError as exc:
            if error_code(exc) in _NO_SUCH_OBJECT:
                raise APIError(ErrorType.NOT_FOUND, EXPIRED_MESSAGE) from exc
            raise normalise_boto_error(exc, action=S3_READ_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=S3_READ_ACTION) from exc
        return bytes(body)


__all__ = [
    "DEFAULT_RULE_SET",
    "EXPIRED_MESSAGE",
    "MAX_MESSAGE_BYTES",
    "RULE_NAME_PREFIX",
    "S3InboundStore",
    "SESInboundProvisioner",
    "bucket_policy",
    "receipt_rule",
]
