"""A fake email provider.

Substituted for ``SESProvider`` so tests can put the account in any state -
sandboxed, production, throttled, denied - without AWS and without moto, which
does not implement the SESv2 ``GetAccount`` call this phase depends on (see
``test_ses_moto.py``).

It satisfies the same Protocol as the real adapter, so a signature drift in
Phase 5 or 6 breaks here too rather than only in production.
"""

from __future__ import annotations

from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import (
    AccountStatus,
    CredentialMode,
    DomainStatus,
    OutboundEmail,
    SendingQuota,
    SentMessage,
)

ACCOUNT_ID = "123456789012"

SANDBOX_QUOTA = SendingQuota(max_24_hour_send=200.0, max_send_rate=1.0, sent_last_24_hours=0.0)
PRODUCTION_QUOTA = SendingQuota(
    max_24_hour_send=50000.0, max_send_rate=14.0, sent_last_24_hours=1200.0
)


class FakeProvider:
    """Answers ``verify_account`` with whatever the test asked for."""

    def __init__(
        self,
        region: str,
        *,
        sandbox: bool = True,
        sending_enabled: bool = True,
        account_id: str = ACCOUNT_ID,
        error: APIError | None = None,
    ) -> None:
        self.region = region
        self.sandbox = sandbox
        self.sending_enabled = sending_enabled
        self.account_id = account_id
        self.error = error
        #: How many times AWS was actually asked. The refresh interval is only
        #: meaningful if a test can see that a call did not happen.
        self.calls = 0

    async def verify_account(self) -> AccountStatus:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AccountStatus(
            account_id=self.account_id,
            region=self.region,
            sandbox=self.sandbox,
            sending_enabled=self.sending_enabled,
            enforcement_status="HEALTHY",
            quota=SANDBOX_QUOTA if self.sandbox else PRODUCTION_QUOTA,
            credential_mode=CredentialMode.ENVIRONMENT,
        )

    async def get_sending_quota(self) -> SendingQuota:
        return (await self.verify_account()).quota

    async def get_domain_status(self, domain: str) -> DomainStatus:
        raise NotImplementedError("Phase 5.")

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        raise NotImplementedError("Phase 6.")


class FakeProviderFactory:
    """A ``ProviderFactory`` that hands back one long-lived provider.

    One instance rather than a fresh one per call, so ``provider.calls`` counts
    across a whole test. That is what makes "the refresh interval stopped a
    second call to AWS" observable - a factory that rebuilt each time would
    reset the count and the test would pass whether or not the guard worked.
    """

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        self.provider = FakeProvider("", **self._kwargs)  # type: ignore[arg-type]
        #: How many times a route asked for a provider at all.
        self.builds = 0

    def __call__(self, region: str) -> FakeProvider:
        self.builds += 1
        self.provider.region = region
        return self.provider


def denied(action: str = "ses:GetAccount") -> APIError:
    """The error the real adapter raises when the IAM policy is too narrow."""
    return APIError(
        ErrorType.AUTHORIZATION_FAILED,
        f"The AWS identity is not permitted to call {action}. "
        f"Add {action} to its IAM policy and try again.",
    )
