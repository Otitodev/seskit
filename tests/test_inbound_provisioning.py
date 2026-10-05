"""Creating and removing the plumbing that brings received mail in.

Against moto, so what is asserted is the state AWS is left in - a bucket that
refuses public access, a rule that sits in the account's own rule set - rather
than that the right boto3 methods were called, which is a test of the test.

The property most worth guarding is the one a happy-path test never touches:
**SESKit must not replace an account's active rule set.** An account has one,
the mail flowing through it belongs to somebody, and replacing it would switch
that off with no error anywhere. ``test_an_existing_active_rule_set_is_never_replaced``
is that guard; it fails against an adapter that creates its own set and
activates it unconditionally.

**moto 5.2.3, checked 2026-09-29.** Everything receiving needs is implemented
except one call, the same shape of gap ``test_event_provisioning.py`` records:

* S3 (bucket, public access block, encryption, lifecycle, policy), SNS, SQS,
  STS, and SES rule sets, ``CreateReceiptRule`` and ``UpdateReceiptRule``:
  implemented.
* SES ``DeleteReceiptRule``: **not implemented.**

``DeleteReceiptRule`` is covered by a shim that removes the rule from moto's own
rule list, so the state teardown leaves behind is still asserted.
``test_moto_still_lacks_delete_receipt_rule`` is the canary that says when the
shim can be deleted.

Two more moto limits worth knowing. It rejects a rule whose bucket or topic does
not exist yet, which matches AWS and is why provisioning must run first. And it
applies a bucket policy's ``Deny`` without evaluating its ``Condition``, so once
the TLS-only statement is in place it refuses *every* object write, TLS or not;
tests that put objects into a provisioned bucket drop the policy first.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("moto", reason="moto is a dev dependency")

import boto3
from botocore.exceptions import ClientError
from fakes.ses import FAKE_CREDENTIALS
from moto import mock_aws
from moto.ses.models import ses_backends
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import InboundInfrastructure, InboundProvisioner, InboundStore
from seskit_provider_aws_ses import (
    DEFAULT_RULE_SET,
    EXPIRED_MESSAGE,
    RULE_NAME_PREFIX,
    S3InboundStore,
    SESInboundProvisioner,
    bucket_policy,
    receipt_rule,
)

REGION = "us-east-1"
ACCOUNT = "123456789012"  # moto's default account
BUCKET = "seskit-inbound-test"
TOPIC = "seskit-inbound"
QUEUE = "seskit-inbound"
RULE = "seskit-example-com"
DOMAIN = "example.com"


@pytest.fixture(autouse=True)
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fake credentials, so a real profile on this machine cannot be used, and
    every AWS call answered by moto.
    """
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


class _WithDeleteReceiptRule:
    """moto's classic SES client, with the one call it lacks done by hand.

    Everything else is delegated, so rules really are created in moto and the
    test can see them. ``delete_receipt_rule`` removes the rule from moto's own
    rule list - what the real call would leave behind - which is the state
    worth asserting.
    """

    def __init__(self, inner: Any, region: str) -> None:
        self._inner = inner
        self._region = region

    def delete_receipt_rule(self, *, RuleSetName: str, RuleName: str) -> dict[str, Any]:
        rule_set = ses_backends[ACCOUNT][self._region].receipt_rule_set.get(RuleSetName)
        if rule_set is None:
            raise ClientError(
                {"Error": {"Code": "RuleSetDoesNotExist", "Message": "gone"}}, "DeleteReceiptRule"
            )
        rule_set.rules = [r for r in rule_set.rules if r["Name"] != RuleName]
        return {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture(autouse=True)
def delete_receipt_rule_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    original = SESInboundProvisioner._receipt

    def receipt(self: SESInboundProvisioner) -> Any:
        return _WithDeleteReceiptRule(original(self), self.region)

    monkeypatch.setattr(SESInboundProvisioner, "_receipt", receipt)


def _provisioner(region: str = REGION) -> SESInboundProvisioner:
    return SESInboundProvisioner(region, FAKE_CREDENTIALS)


async def _provision(
    provisioner: SESInboundProvisioner | None = None, *, retention_days: int = 30
) -> InboundInfrastructure:
    return await (provisioner or _provisioner()).provision_inbound(
        bucket_name=BUCKET, topic_name=TOPIC, queue_name=QUEUE, retention_days=retention_days
    )


def _rule_names(rule_set: str) -> list[str]:
    described = boto3.client("ses", region_name=REGION).describe_receipt_rule_set(
        RuleSetName=rule_set
    )
    return [rule["Name"] for rule in described["Rules"]]


def _active_name() -> str | None:
    active = boto3.client("ses", region_name=REGION).describe_active_receipt_rule_set()
    return (active.get("Metadata") or {}).get("Name")


def _make_users_rule_set(name: str = "my-existing-rules", *, rule: str = "forward-to-me") -> None:
    """An account that already receives mail, the way a real one would."""
    client = boto3.client("ses", region_name=REGION)
    client.create_receipt_rule_set(RuleSetName=name)
    client.create_receipt_rule(
        RuleSetName=name,
        Rule={
            "Name": rule,
            "Enabled": True,
            "Recipients": ["support@example.org"],
            "Actions": [{"StopAction": {"Scope": "RuleSet"}}],
        },
    )
    client.set_active_receipt_rule_set(RuleSetName=name)


# ------------------------------------------------------------------- pure ---


def test_the_adapters_satisfy_the_provider_protocols() -> None:
    assert isinstance(_provisioner(), InboundProvisioner)
    assert isinstance(S3InboundStore(REGION, FAKE_CREDENTIALS), InboundStore)


def test_the_bucket_policy_admits_this_accounts_seskit_rules_only() -> None:
    """Without both conditions any receipt rule in the account - including one
    somebody else made - could write into the bucket.
    """
    policy = json.loads(bucket_policy(bucket=BUCKET, region=REGION, account_id=ACCOUNT))
    allow = next(s for s in policy["Statement"] if s["Effect"] == "Allow")

    assert allow["Principal"] == {"Service": "ses.amazonaws.com"}
    assert allow["Action"] == "s3:PutObject"
    assert allow["Condition"]["StringEquals"]["AWS:SourceAccount"] == ACCOUNT
    source = allow["Condition"]["ArnLike"]["AWS:SourceArn"]
    assert source == (
        f"arn:aws:ses:{REGION}:{ACCOUNT}:receipt-rule-set/*:receipt-rule/{RULE_NAME_PREFIX}*"
    )


def test_the_bucket_policy_refuses_plain_http() -> None:
    policy = json.loads(bucket_policy(bucket=BUCKET, region=REGION, account_id=ACCOUNT))
    deny = next(s for s in policy["Statement"] if s["Effect"] == "Deny")

    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_a_rule_stores_the_message_then_announces_it() -> None:
    rule = receipt_rule(name=RULE, domain=DOMAIN, bucket=BUCKET, topic_arn="arn:topic")

    assert rule["Recipients"] == [DOMAIN]
    assert rule["ScanEnabled"] is True
    (action,) = rule["Actions"]
    assert action["S3Action"] == {
        "BucketName": BUCKET,
        "ObjectKeyPrefix": f"{RULE}/",
        "TopicArn": "arn:topic",
    }
    # The whole message in the SNS notification is capped at 150 KB, and the
    # S3 route is what allows 40 MB. There must be no SNSAction beside it.
    assert all("SNSAction" not in a for a in rule["Actions"])
    # And no Stop action: this rule must never change what another does.
    assert all("StopAction" not in a for a in rule["Actions"])


# -------------------------------------------------------------- provision ---


async def test_provisioning_creates_a_private_encrypted_expiring_bucket() -> None:
    await _provision(retention_days=45)
    s3 = boto3.client("s3", region_name=REGION)

    block = s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]
    assert all(block.values())

    encryption = s3.get_bucket_encryption(Bucket=BUCKET)["ServerSideEncryptionConfiguration"]
    assert encryption["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"

    (rule,) = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert rule["Expiration"]["Days"] == 45
    assert rule["Status"] == "Enabled"

    policy = json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"])
    assert {s["Sid"] for s in policy["Statement"]} == {"seskit-allow-ses-puts", "seskit-tls-only"}


async def test_provisioning_wires_the_topic_to_the_queue() -> None:
    infrastructure = await _provision()

    assert infrastructure.bucket == BUCKET
    assert infrastructure.topic_arn.endswith(f":{TOPIC}")
    assert infrastructure.queue_arn.endswith(f":{QUEUE}")
    assert infrastructure.subscription_arn.startswith("arn:")

    sns = boto3.client("sns", region_name=REGION)
    subscriptions = sns.list_subscriptions_by_topic(TopicArn=infrastructure.topic_arn)
    (subscription,) = subscriptions["Subscriptions"]
    assert subscription["Endpoint"] == infrastructure.queue_arn
    # Raw delivery must stay off: the SNS envelope's MessageId is what the
    # worker deduplicates on, and raw delivery strips it.
    attributes = sns.get_subscription_attributes(SubscriptionArn=infrastructure.subscription_arn)
    assert attributes["Attributes"]["RawMessageDelivery"] == "false"


async def test_provisioning_twice_leaves_one_set_of_resources() -> None:
    first = await _provision()
    second = await _provision()

    assert second == first
    assert len(boto3.client("s3", region_name=REGION).list_buckets()["Buckets"]) == 1
    assert len(boto3.client("sns", region_name=REGION).list_topics()["Topics"]) == 1


async def test_provisioning_again_applies_a_changed_retention() -> None:
    await _provision(retention_days=30)
    await _provision(retention_days=7)

    s3 = boto3.client("s3", region_name=REGION)
    (rule,) = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert rule["Expiration"]["Days"] == 7


async def test_a_bucket_outside_us_east_1_is_created_in_its_own_region() -> None:
    """us-east-1 is the one region that refuses a location constraint, and every
    other region requires one.
    """
    provisioner = _provisioner("eu-west-1")
    await provisioner.provision_inbound(
        bucket_name=BUCKET, topic_name=TOPIC, queue_name=QUEUE, retention_days=30
    )

    location = boto3.client("s3", region_name="eu-west-1").get_bucket_location(Bucket=BUCKET)
    assert location["LocationConstraint"] == "eu-west-1"


async def test_retention_below_a_day_is_refused() -> None:
    with pytest.raises(APIError) as caught:
        await _provision(retention_days=0)

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []


# ------------------------------------------------------------------ rules ---


async def test_with_no_active_rule_set_one_is_created_and_activated() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    rule = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    assert rule.rule_set == DEFAULT_RULE_SET
    assert rule.created_rule_set is True
    assert _active_name() == DEFAULT_RULE_SET
    assert _rule_names(DEFAULT_RULE_SET) == [RULE]


async def test_an_existing_active_rule_set_is_never_replaced() -> None:
    """The guard. An account that already receives mail keeps receiving it:
    its rule set stays active, its rule stays in it and stays first-class, and
    ours is added alongside.
    """
    _make_users_rule_set()
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    rule = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    assert _active_name() == "my-existing-rules"
    assert rule.rule_set == "my-existing-rules"
    assert rule.created_rule_set is False
    assert set(_rule_names("my-existing-rules")) == {"forward-to-me", RULE}
    assert DEFAULT_RULE_SET not in [
        s["Name"]
        for s in boto3.client("ses", region_name=REGION).list_receipt_rule_sets()["RuleSets"]
    ]


async def test_our_rule_goes_ahead_of_a_stop_action_it_would_otherwise_sit_behind() -> None:
    """A rule with no ``After`` is inserted first. Placed last it could sit behind
    somebody's Stop action and never run, and nothing would say so.
    """
    _make_users_rule_set()
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    assert _rule_names("my-existing-rules")[0] == RULE


async def test_adding_the_same_rule_twice_leaves_one() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)
    again = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    assert _rule_names(DEFAULT_RULE_SET) == [RULE]
    # A set SESKit made on an earlier run is still SESKit's, so a repair run
    # does not forget that it may remove it.
    assert again.created_rule_set is True


async def test_a_second_domain_adds_its_rule_beside_the_first() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)
    await provisioner.add_inbound_rule(
        infrastructure, domain="example.net", rule_name="seskit-example-net"
    )

    assert set(_rule_names(DEFAULT_RULE_SET)) == {RULE, "seskit-example-net"}


async def test_a_rule_name_the_bucket_policy_would_refuse_is_rejected_up_front() -> None:
    """SES would accept the message and the bucket would refuse the write, so
    the mail would be lost with no error at any step. This is the one place
    that failure can be seen.
    """
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    with pytest.raises(APIError) as caught:
        await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name="my-rule")

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert _active_name() is None


# -------------------------------------------------------------- remove rule ---


async def test_removing_our_last_rule_removes_the_set_we_made() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)
    rule = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    await provisioner.remove_inbound_rule(rule)

    assert _active_name() is None
    names = boto3.client("ses", region_name=REGION).list_receipt_rule_sets()["RuleSets"]
    assert names == []


async def test_a_set_we_made_stays_while_another_domain_still_uses_it() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)
    first = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)
    await provisioner.add_inbound_rule(
        infrastructure, domain="example.net", rule_name="seskit-example-net"
    )

    await provisioner.remove_inbound_rule(first)

    assert _active_name() == DEFAULT_RULE_SET
    assert _rule_names(DEFAULT_RULE_SET) == ["seskit-example-net"]


async def test_removing_our_rule_leaves_the_users_rule_set_alone() -> None:
    """Even if ours was the only rule SESKit put there, the set is theirs."""
    _make_users_rule_set()
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)
    rule = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    await provisioner.remove_inbound_rule(rule)

    assert _active_name() == "my-existing-rules"
    assert _rule_names("my-existing-rules") == ["forward-to-me"]


async def test_removing_a_rule_that_is_already_gone_is_not_an_error() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)
    rule = await provisioner.add_inbound_rule(infrastructure, domain=DOMAIN, rule_name=RULE)

    await provisioner.remove_inbound_rule(rule)
    await provisioner.remove_inbound_rule(rule)


# ---------------------------------------------------------- remove inbound ---


async def test_removing_an_empty_installation_removes_everything() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    bucket_removed = await provisioner.remove_inbound(infrastructure)

    assert bucket_removed is True
    assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []
    assert boto3.client("sns", region_name=REGION).list_topics()["Topics"] == []
    assert boto3.client("sqs", region_name=REGION).list_queues().get("QueueUrls", []) == []


async def test_a_bucket_holding_mail_is_kept() -> None:
    """Switching receiving off must not delete what has been received. The topic
    and queue go; the bucket stays and its expiry empties it.
    """
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)
    s3 = boto3.client("s3", region_name=REGION)
    # moto ignores the TLS-only statement's condition and would refuse this
    # write; the test is about teardown, not about the policy.
    s3.delete_bucket_policy(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key=f"{RULE}/abc", Body=b"Subject: hi\r\n\r\nhello")

    bucket_removed = await provisioner.remove_inbound(infrastructure)

    assert bucket_removed is False
    assert boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET)["KeyCount"] == 1
    assert boto3.client("sns", region_name=REGION).list_topics()["Topics"] == []


async def test_removing_what_is_already_gone_is_not_an_error() -> None:
    provisioner = _provisioner()
    infrastructure = await _provision(provisioner)

    await provisioner.remove_inbound(infrastructure)
    assert await provisioner.remove_inbound(infrastructure) is True


# ------------------------------------------------------------------ store ---


async def test_a_stored_message_comes_back_byte_for_byte() -> None:
    """Bytes, not text: a message with an 8-bit body must survive unchanged,
    because the parser downstream is what decides what it means.
    """
    raw = b"From: a@example.org\r\nSubject: caf\xc3\xa9\r\n\r\nbody \xff\xfe\r\n"
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key=f"{RULE}/abc", Body=raw)

    fetched = await S3InboundStore(REGION, FAKE_CREDENTIALS).fetch_message(
        bucket=BUCKET, key=f"{RULE}/abc"
    )

    assert fetched == raw


async def test_an_expired_message_says_so_rather_than_failing_generically() -> None:
    """Expired is settled and will never come back; unreadable may succeed on a
    retry. The caller has to be able to tell which it is holding.
    """
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)

    with pytest.raises(APIError) as caught:
        await S3InboundStore(REGION, FAKE_CREDENTIALS).fetch_message(
            bucket=BUCKET, key=f"{RULE}/gone"
        )

    assert caught.value.error_type is ErrorType.NOT_FOUND
    assert caught.value.message == EXPIRED_MESSAGE


# ----------------------------------------------------------------- canary ---


def test_moto_still_lacks_delete_receipt_rule() -> None:
    """Fails the day moto implements the call. When it does, delete
    ``_WithDeleteReceiptRule`` and the fixture that installs it: the tests above
    will then be exercising moto's own deletion instead of a hand-written one.
    """
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    boto3.client("sns", region_name=REGION).create_topic(Name=TOPIC)
    client = boto3.client("ses", region_name=REGION)
    client.create_receipt_rule_set(RuleSetName="canary")

    with pytest.raises(NotImplementedError):
        client.delete_receipt_rule(RuleSetName="canary", RuleName="anything")
