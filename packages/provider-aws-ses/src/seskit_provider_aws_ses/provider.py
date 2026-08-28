"""The Amazon SES adapter.

Implements the account-level half of :class:`~seskit_core.providers.EmailProvider`
(§26). Sending arrives in Phase 6 and domain identities in Phase 5; those
methods are declared on the Protocol and raise here until then, so an accidental
early call fails loudly rather than returning something plausible.

Everything AWS-shaped stops at this boundary. Callers get core's dataclasses and
core's ``APIError``, never a boto3 response dict and never a ``ClientError``.

**A note on testing, from the Phase 4 spike (moto 5.2.3):** moto does *not*
implement SESv2 ``GetAccount`` - it raises ``NotImplementedError``. That is the
one call carrying both the sandbox flag and the send quota. SES v1's
``GetSendQuota`` is mocked, but v1 has no ``ProductionAccessEnabled``
equivalent, so falling back to it would buy a mockable quota at the cost of a
second client and still leave sandbox detection unmocked. ``GetAccount`` is
therefore kept as the production call, and its behaviour is covered by a fake
client rather than by moto. Do not "fix" this by switching to v1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from seskit_core.email import build_message
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.providers.types import (
    AccountStatus,
    CredentialMode,
    IdentityStatus,
    IdentityType,
    OutboundEmail,
    SendingQuota,
    SentMessage,
)

from seskit_provider_aws_ses.client import (
    BOTO_CONFIG,
    build_session,
    call,
    resolve_credential_mode,
)
from seskit_provider_aws_ses.errors import error_code, normalise_boto_error
from seskit_provider_aws_ses.identities import to_identity_status

if TYPE_CHECKING:
    # Types only, from boto3-stubs. Importing these at runtime would make a
    # dev-only dependency a production one.
    from mypy_boto3_sesv2.type_defs import GetAccountResponseTypeDef

logger = get_logger(__name__)

STS_IDENTITY_ACTION = "sts:GetCallerIdentity"
SES_ACCOUNT_ACTION = "ses:GetAccount"
SES_CREATE_IDENTITY_ACTION = "ses:CreateEmailIdentity"
SES_GET_IDENTITY_ACTION = "ses:GetEmailIdentity"
SES_DELETE_IDENTITY_ACTION = "ses:DeleteEmailIdentity"
SES_SEND_ACTION = "ses:SendEmail"

#: SES says the identity is already there. Not a failure - see create_identity.
_ALREADY_EXISTS_CODES = frozenset({"AlreadyExistsException"})

#: Already gone. Also not a failure - see delete_identity.
_NOT_FOUND_CODES = frozenset({"NotFoundException"})


class SESProvider:
    """Talks to one AWS account, in one region.

    Constructed per request rather than held as a long-lived singleton. boto3
    clients are cheap to build, region varies per project, and a cached client
    would keep serving a role whose credentials have since rotated.
    """

    def __init__(self, region: str) -> None:
        self.region = region
        self._session = build_session(region)

    # ------------------------------------------------------------ account ---

    async def verify_account(self) -> AccountStatus:
        """Prove the identity is real and report what it may do.

        Two calls, in this order. ``GetCallerIdentity`` first because it is the
        cheapest possible proof that credentials resolve at all and needs no SES
        permission - so "no credentials" and "no SES permission" come back as
        different, actionable errors instead of one confusing failure.
        """
        account_id = await self._account_id()
        account = await self._get_account()

        quota = account.get("SendQuota") or {}

        return AccountStatus(
            account_id=account_id,
            region=self.region,
            # ProductionAccessEnabled is the sandbox flag inverted. Absent means
            # sandboxed: the safe reading, since claiming production access the
            # account does not have is what §8 is trying to prevent.
            sandbox=not bool(account.get("ProductionAccessEnabled", False)),
            sending_enabled=bool(account.get("SendingEnabled", False)),
            enforcement_status=str(account.get("EnforcementStatus", "")),
            quota=SendingQuota(
                max_24_hour_send=float(quota.get("Max24HourSend", 0.0)),
                max_send_rate=float(quota.get("MaxSendRate", 0.0)),
                sent_last_24_hours=float(quota.get("SentLast24Hours", 0.0)),
            ),
            credential_mode=self.credential_mode,
        )

    async def get_sending_quota(self) -> SendingQuota:
        """The current allowance.

        Same ``GetAccount`` call as :meth:`verify_account`, because SESv2
        returns the quota inside it - there is no cheaper quota-only call in v2
        worth a second code path.
        """
        return (await self.verify_account()).quota

    @property
    def credential_mode(self) -> CredentialMode:
        return resolve_credential_mode(self._session)

    # ----------------------------------------------------------- identities ---

    async def create_identity(self, value: str, identity_type: IdentityType) -> IdentityStatus:
        """Start verifying a domain or an email address.

        An identity that already exists is not an error. SES identities belong
        to the account and region, so a second project adding a domain the first
        already verified must adopt that state rather than being told no - and
        must certainly not be shown DNS records that are already published.
        """
        client = self._session.client("sesv2", config=BOTO_CONFIG)

        try:
            response = await call(client.create_email_identity, EmailIdentity=value)
        except ClientError as exc:
            if error_code(exc) in _ALREADY_EXISTS_CODES:
                return await self.get_identity_status(value)
            raise normalise_boto_error(exc, action=SES_CREATE_IDENTITY_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_CREATE_IDENTITY_ACTION) from exc

        return to_identity_status(value, dict(response), fallback_type=identity_type)

    async def get_identity_status(self, value: str) -> IdentityStatus:
        """Current verification, DKIM and MAIL FROM state."""
        client = self._session.client("sesv2", config=BOTO_CONFIG)

        try:
            response = await call(client.get_email_identity, EmailIdentity=value)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_GET_IDENTITY_ACTION) from exc

        return to_identity_status(value, dict(response), fallback_type=_guess_type(value))

    async def delete_identity(self, value: str) -> None:
        """Remove the identity at SES.

        An identity that is already gone is a success, not a failure - the
        caller wanted it absent and it is. Treating it as an error would leave
        a SESKit row that can never be cleaned up.
        """
        client = self._session.client("sesv2", config=BOTO_CONFIG)

        try:
            await call(client.delete_email_identity, EmailIdentity=value)
        except ClientError as exc:
            if error_code(exc) in _NOT_FOUND_CODES:
                return
            raise normalise_boto_error(exc, action=SES_DELETE_IDENTITY_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_DELETE_IDENTITY_ACTION) from exc

    # ----------------------------------------------------------------- send ---

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        """Hand a message to SES.

        Simple content when it will do, raw MIME when it will not. Simple lets
        SES assemble the message from structured fields, which it handles better
        than a blob - but it can only express what its own fields cover, so
        attachments and custom headers force the raw path.

        ``Destination`` is passed either way. With raw content SES would
        otherwise take recipients from the headers, and blind copies are
        deliberately not in the headers.
        """
        client = self._session.client("sesv2", config=BOTO_CONFIG)
        request: dict[str, Any] = {
            "FromEmailAddress": message.sender,
            "Destination": {
                "ToAddresses": list(message.to),
                "CcAddresses": list(message.cc),
                "BccAddresses": list(message.bcc),
            },
            "Content": _content(message),
        }
        if message.reply_to:
            request["ReplyToAddresses"] = list(message.reply_to)

        try:
            response = await call(client.send_email, **request)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_SEND_ACTION) from exc

        return SentMessage(provider_message_id=str(response.get("MessageId", "")))

    # ------------------------------------------------------------ internal ---

    async def _account_id(self) -> str:
        client = self._session.client("sts", config=BOTO_CONFIG)
        try:
            identity = await call(client.get_caller_identity)
        except Exception as exc:
            raise normalise_boto_error(exc, action=STS_IDENTITY_ACTION) from exc

        account_id = identity.get("Account")
        if not account_id:
            # Shouldn't happen against real STS, but an empty account id stored
            # as though it were real would be worse than an explicit failure.
            raise APIError(
                ErrorType.PROVIDER_ERROR,
                "AWS did not return an account identifier.",
            )
        return str(account_id)

    async def _get_account(self) -> GetAccountResponseTypeDef:
        client = self._session.client("sesv2", config=BOTO_CONFIG)
        try:
            account = await call(client.get_account)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_ACCOUNT_ACTION) from exc
        return account


def _guess_type(value: str) -> IdentityType:
    """Fallback only, for a response that does not say what it is.

    SES reports the real type and that is what wins; this exists so the
    dataclass always has one.
    """
    return IdentityType.EMAIL_ADDRESS if "@" in value else IdentityType.DOMAIN


def _needs_raw(message: OutboundEmail) -> bool:
    """Whether SES has to be given the assembled MIME rather than fields.

    Simple content is built by SES from what we pass, so anything it has no
    field for - an attachment, a custom header - cannot survive that path.
    """
    return bool(message.attachments or message.headers)


def _content(message: OutboundEmail) -> dict[str, Any]:
    if _needs_raw(message):
        return {"Raw": {"Data": build_message(message).as_bytes()}}

    body: dict[str, Any] = {}
    if message.text is not None:
        body["Text"] = {"Data": message.text, "Charset": "UTF-8"}
    if message.html is not None:
        body["Html"] = {"Data": message.html, "Charset": "UTF-8"}

    return {
        "Simple": {
            "Subject": {"Data": message.subject, "Charset": "UTF-8"},
            "Body": body,
        }
    }
