"""The provider interface (§26).

§26 asks for this abstraction even though SES is the only production provider in
the MVP, so that Postmark, Mailgun or SendGrid could arrive later without the
call sites changing. It earns its place sooner than that: Phase 6 adds an
``SMTPProvider`` for local development (§25), and it satisfies this same
interface, so nothing above the provider layer has to know which one is in use.

A ``Protocol`` rather than an abstract base class. Adapters live in their own
packages (``seskit-provider-aws-ses``) and are never imported by core, so
structural typing is what keeps the dependency pointing the right way: the
adapter depends on core, core knows only the shape.

The interface lives in core rather than in the provider package because core is
what the API and the service layer import. Putting it beside the SES adapter
would mean core importing a provider to type a variable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from seskit_core.providers.types import (
    AccountStatus,
    IdentityStatus,
    IdentityType,
    OutboundEmail,
    SendingQuota,
    SentMessage,
)


@runtime_checkable
class EmailProvider(Protocol):
    """What every sending backend must be able to do.

    Phase 4 implements only the two account-level calls. The domain and send
    methods are declared now so the shape is settled once, in the same spirit as
    ``IDPrefix`` declaring prefixes for phases that have not arrived - a later
    phase should find the name already chosen rather than coin a second one
    beside it.

    Every method is async. The SES adapter's underlying client is synchronous
    boto3 and hands off to a thread; a future HTTP-based provider would be
    natively async. Callers should not have to know which.
    """

    async def verify_account(self) -> AccountStatus:
        """Prove the configured identity can use this provider, and report what
        it is allowed to do.

        Raises a normalised ``APIError`` rather than a provider-native exception
        (§19).
        """
        ...

    async def get_sending_quota(self) -> SendingQuota:
        """The current sending allowance."""
        ...

    async def create_identity(self, value: str, identity_type: IdentityType) -> IdentityStatus:
        """Ask the provider to start verifying a domain or an email address.

        Idempotent by nature: an identity that already exists comes back with
        its current state rather than being reset, which is what lets a second
        project adopt a domain the first has already verified.
        """
        ...

    async def get_identity_status(self, value: str) -> IdentityStatus:
        """Current verification, DKIM and MAIL FROM state for one identity."""
        ...

    async def delete_identity(self, value: str) -> None:
        """Remove the identity at the provider.

        Callers must be sure nothing else is relying on it - see the refcount
        in ``services.identities``.
        """
        ...

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        """Phase 6. Hand one message to the provider."""
        ...
