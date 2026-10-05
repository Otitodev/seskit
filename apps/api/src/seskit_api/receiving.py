"""What the Domains page shows about receiving mail for one domain.

A view model and nothing more: it decides whether receiving can be turned on for a
domain *right now*, and if not, says why in words a person can act on. The page
renders it, and the route that turns receiving on asks it again before doing
anything - the disabled button is a courtesy, and this is the check, because a
form can be posted by hand.

**Every reason is stated rather than hidden.** A button that is simply absent leaves
somebody wondering whether the feature exists. Each of these is a precondition they
can fix, and the page says which.

The MX record is built here and not in a template so that a test can assert on it.
It is the one thing a user has to do by hand, outside SESKit, and a record with the
wrong host would send their mail nowhere without an error anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from seskit_core.config import Settings
from seskit_core.models import AWSConnection, Identity
from seskit_provider_aws_ses import supports_receiving

#: SES takes mail at the lowest-preference host. One host, so one value; the number
#: only matters when a domain lists several, and nobody should be listing others
#: beside this one.
MX_PRIORITY = 10

#: Where Amazon SES takes mail in a region. Published by AWS in the General
#: Reference's "Email Receiving endpoints" table, one per receiving region.
MX_HOST = "inbound-smtp.{region}.amazonaws.com"


@dataclass(frozen=True, slots=True)
class MxRecord:
    """The record to add at the user's DNS provider."""

    name: str
    priority: int
    value: str

    @property
    def zone_line(self) -> str:
        """As one line, for a provider that takes a zone file."""
        return f"{self.name}  MX  {self.priority} {self.value}"


@dataclass(frozen=True, slots=True)
class ReceivingView:
    """Receiving for one domain, as the page needs it."""

    enabled: bool
    #: Why it cannot be turned on right now, or ``None`` if it can. Always ``None``
    #: when it is already on - a running thing is not blocked, and the controls
    #: that matter then are the ones to stop it.
    blocked: str | None
    mx: MxRecord
    retention_days: int


def mx_record(identity: Identity) -> MxRecord:
    return MxRecord(
        name=identity.value,
        priority=MX_PRIORITY,
        value=MX_HOST.format(region=identity.region),
    )


def blocked_reason(
    identity: Identity,
    connection: AWSConnection | None,
    settings: Settings,
    *,
    taken_elsewhere: bool,
) -> str | None:
    """The first thing in the way of turning receiving on, in the order a person
    would fix them, or ``None`` if nothing is.
    """
    if not identity.is_verified:
        return "Verify this domain first. Receiving needs a domain Amazon SES knows is yours."

    if connection is None or not connection.is_connected or not connection.has_credentials:
        return "Connect AWS first, on the AWS page."

    if identity.region != connection.region:
        return (
            f"This domain is verified in {identity.region}, but this project is connected to "
            f"{connection.region}. Mail is received in the connection's region."
        )

    if not supports_receiving(identity.region):
        return (
            f"Amazon SES cannot receive mail in {identity.region}. It can in regions such as "
            "us-east-1, eu-west-1 and ap-southeast-2."
        )

    if not settings.polls_sqs:
        return (
            "Receiving needs this instance to read from SQS, and EVENT_INGESTION is set to "
            "https. Set it to sqs or both."
        )

    if taken_elsewhere:
        return (
            "This domain already receives mail for another project or region. A domain's MX "
            "record can only point at one place."
        )

    return None


def receiving_view(
    identity: Identity,
    connection: AWSConnection | None,
    settings: Settings,
    *,
    taken_elsewhere: bool = False,
) -> ReceivingView | None:
    """``None`` for something that cannot receive at all - an email address has no
    MX record to point anywhere - so the page shows nothing rather than a control
    that could never work.
    """
    if not identity.is_domain:
        return None

    enabled = identity.receives_mail
    return ReceivingView(
        enabled=enabled,
        blocked=None
        if enabled
        else blocked_reason(identity, connection, settings, taken_elsewhere=taken_elsewhere),
        mx=mx_record(identity),
        retention_days=settings.INBOUND_RETENTION_DAYS,
    )
