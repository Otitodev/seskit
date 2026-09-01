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
    EventInfrastructure,
    IdentityStatus,
    IdentityType,
    OutboundEmail,
    QueuedNotification,
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


@runtime_checkable
class EventProvisioner(Protocol):
    """Creating and removing the plumbing that carries events back (§15).

    Separate from :class:`EmailProvider` on purpose. Sending and provisioning
    are different capabilities: SMTP can send and has no notion of a topic or a
    queue, and folding these methods into the sending interface would make the
    local development provider implement three no-ops to satisfy a shape it can
    never honour.

    Provisioning is what makes SESKit's thesis true. A user should not have to
    learn what a configuration set is to find out that their mail bounced.
    """

    async def provision_events(
        self,
        *,
        queue_name: str,
        topic_name: str,
        configuration_set: str,
        https_endpoint: str | None = None,
        track_opens_and_clicks: bool = False,
    ) -> EventInfrastructure:
        """Create everything needed for events to reach SESKit.

        Idempotent. Running it twice must converge on the same infrastructure
        rather than creating a second copy - a user who clicks the button again
        because nothing seemed to happen should not end up with two topics.
        """
        ...

    async def remove_events(self, infrastructure: EventInfrastructure) -> None:
        """Remove exactly what :meth:`provision_events` created.

        Only what is named in ``infrastructure``. Callers must have checked
        that no other project still depends on it - see the refcount in
        ``services.events``, which exists for the same reason as the one in
        ``services.identities``.
        """
        ...

    async def set_open_click_tracking(
        self, infrastructure: EventInfrastructure, *, enabled: bool
    ) -> EventInfrastructure:
        """Turn open and click reporting on or off for the configuration set."""
        ...


@runtime_checkable
class NotificationQueue(Protocol):
    """Reading provider notifications off a queue (§15).

    A third capability rather than more methods on the other two, for the same
    reason they are separate: a provider that can send has no reason to be able
    to poll, and an interface that demands both makes every implementation
    carry methods it cannot honour.
    """

    async def receive(
        self,
        *,
        max_messages: int = 10,
        wait_seconds: int = 20,
        visibility_timeout: int = 60,
    ) -> list[QueuedNotification]:
        """Take up to a batch off the queue, waiting if it is empty."""
        ...

    async def delete(self, notification: QueuedNotification) -> None:
        """Acknowledge one message, so it is not delivered again."""
        ...
