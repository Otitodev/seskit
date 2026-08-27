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

from typing import TYPE_CHECKING

from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.providers.types import (
    AccountStatus,
    CredentialMode,
    DomainStatus,
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
from seskit_provider_aws_ses.errors import normalise_boto_error

if TYPE_CHECKING:
    # Types only, from boto3-stubs. Importing these at runtime would make a
    # dev-only dependency a production one.
    from mypy_boto3_sesv2.type_defs import GetAccountResponseTypeDef

logger = get_logger(__name__)

STS_IDENTITY_ACTION = "sts:GetCallerIdentity"
SES_ACCOUNT_ACTION = "ses:GetAccount"


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

    # ------------------------------------------------------------- Phase 5 ---

    async def get_domain_status(self, domain: str) -> DomainStatus:
        raise NotImplementedError("Domain identities arrive in Phase 5.")

    # ------------------------------------------------------------- Phase 6 ---

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        raise NotImplementedError("Sending arrives in Phase 6.")

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
