"""Leaving the SES sandbox from the dashboard (§31 Phase 16).

A new SES account sends only to verified addresses, two hundred a day. Leaving
that sandbox is a request AWS reviews by hand, and its form asks the sender to
attest to two things: that they only mail people who asked, and that they have
"a process in place for handling bounce and complaint notifications". AWS also
says, in its own docs, that a verified *domain* is what gets a request approved
quickly.

SESKit holds the state that answers those questions, so it checks them rather
than asking. A request is only submitted once every gate passes; the service
refuses otherwise, naming what is left, and the page's disabled button is a
courtesy on top of that refusal rather than the guard itself. A request AWS
denies on arrival costs the user a day and a Support case - the gates exist so
that the request SESKit sends is one AWS can grant.

The gates are per AWS account and region, because that is what the sandbox is.
A verified domain in a sibling project on the same account counts, exactly as
it counts to SES.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.errors import APIError, ErrorType
from seskit_core.events.origin import TopicOrigin, connections_for_origin
from seskit_core.logging import get_logger
from seskit_core.models import AWSConnection, Email, Identity, utcnow
from seskit_core.providers import (
    ContactLanguage,
    IdentityType,
    MailType,
    ProductionAccessRequest,
    ReviewStatus,
    VerificationStatus,
)
from seskit_core.services.aws import ProviderFactory
from seskit_core.services.credentials import stored_credentials

logger = get_logger(__name__)

#: AWS's own limit on the contact list.
MAX_CONTACTS = 4

#: What AWS asks the sender to confirm, in its words. The form repeats it and
#: the service requires it, so the attestation SESKit makes on the user's
#: behalf is one they actually made.
ACKNOWLEDGEMENT = (
    "I will only send email to individuals who have explicitly requested it, and I "
    "have a process in place for handling bounce and complaint notifications."
)


@dataclass(frozen=True, slots=True)
class Readiness:
    """The prerequisites, each with its evidence.

    Evidence rather than booleans, so the page can say "otito.site is
    verified" and "3 messages delivered" instead of a bare tick - and so a
    refusal can name what is missing.
    """

    #: A verified domain in this account and region, or None. An address
    #: passes SES's rules and does not pass this: AWS says domain.
    verified_domain: str | None
    #: Event reporting set up on this connection. This is the "process in
    #: place for handling bounce and complaint notifications", concretely.
    events_enabled: bool
    #: Messages sent from this account and region that SES has confirmed
    #: delivered. One proves the pipeline works end to end; the number is
    #: shown because it is more convincing than a tick.
    delivered_count: int

    @property
    def has_verified_domain(self) -> bool:
        return self.verified_domain is not None

    @property
    def has_delivered(self) -> bool:
        return self.delivered_count > 0

    @property
    def ready(self) -> bool:
        return self.has_verified_domain and self.events_enabled and self.has_delivered

    @property
    def unmet(self) -> list[str]:
        """What is left, in the order it is done."""
        missing = []
        if not self.has_verified_domain:
            missing.append("a verified domain")
        if not self.events_enabled:
            missing.append("delivery event reporting")
        if not self.has_delivered:
            missing.append("a delivered message")
        return missing


async def production_access_readiness(
    session: AsyncSession, connection: AWSConnection
) -> Readiness:
    """Check the gates against what SESKit knows about this account."""
    origin = TopicOrigin(region=connection.region, account_id=connection.aws_account_id)
    siblings = await connections_for_origin(session, origin)
    project_ids = [sibling.project_id for sibling in siblings] or [connection.project_id]

    domain = await session.scalar(
        select(Identity.value)
        .where(
            Identity.project_id.in_(project_ids),
            Identity.region == connection.region,
            Identity.identity_type == IdentityType.DOMAIN.value,
            Identity.verification_status == VerificationStatus.SUCCESS.value,
        )
        .order_by(Identity.created_at)
        .limit(1)
    )
    delivered = await session.scalar(
        select(func.count(Email.id)).where(
            Email.project_id.in_(project_ids), Email.delivered_at.is_not(None)
        )
    )

    return Readiness(
        verified_domain=domain,
        events_enabled=connection.events_enabled,
        delivered_count=int(delivered or 0),
    )


async def request_production_access(
    session: AsyncSession,
    provider_factory: ProviderFactory,
    connection: AWSConnection,
    *,
    mail_type: MailType,
    website_url: str,
    contact_addresses: list[str],
    contact_language: ContactLanguage = ContactLanguage.EN,
    acknowledged: bool,
    secret_key: str,
) -> Readiness:
    """Ask AWS to take this account out of the sandbox.

    Refuses, with the reason, before touching AWS: unmet gates, a request
    already under review, a URL that is not one, no contact address, or the
    acknowledgement not given. Everything AWS would refuse it for that SESKit
    can see first. Returns the readiness it checked, for the page.
    """
    if not connection.sandbox:
        raise APIError(ErrorType.INVALID_REQUEST, "This account already has production access.")
    if connection.production_access_pending:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "AWS is still reviewing an earlier production access request for this account.",
        )

    readiness = await production_access_readiness(session, connection)
    if not readiness.ready:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "Not ready to request production access. Still needed: "
            + ", ".join(readiness.unmet)
            + ".",
        )

    website_url = website_url.strip()
    contacts = _contacts(contact_addresses)
    _require_url(website_url)
    if not acknowledged:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "AWS requires the acknowledgement. Tick it to confirm it is true.",
        )

    request = ProductionAccessRequest(
        mail_type=mail_type,
        website_url=website_url,
        contact_addresses=contacts,
        contact_language=contact_language,
        use_case_description=describe_use_case(mail_type, readiness),
    )
    provider = provider_factory(
        connection.region, stored_credentials(connection, secret_key=secret_key)
    )
    await provider.request_production_access(request)

    # Recorded before AWS confirms anything on a refresh: the page has to
    # show the wait immediately, and SES refuses a second request meanwhile.
    connection.production_requested_at = utcnow()
    connection.review_status = ReviewStatus.PENDING.value
    await session.flush()

    logger.info(
        "production_access_requested",
        project_id=connection.project_id,
        region=connection.region,
        aws_account_id=connection.aws_account_id,
        mail_type=mail_type.value,
    )
    return readiness


def describe_use_case(mail_type: MailType, readiness: Readiness) -> str:
    """What SESKit tells the reviewer, built from what it knows.

    SES's API takes a free-text use case the console form no longer shows.
    A reviewer who reads it should see the bounce and complaint process
    described in one paragraph, which is what the review is about - and the
    user should not have to describe software they did not write.
    """
    kind = "Transactional" if mail_type is MailType.TRANSACTIONAL else "Marketing"
    return (
        f"{kind} email from the verified domain {readiness.verified_domain}, sent through "
        "SESKit, a self-hosted email platform on Amazon SES "
        "(https://github.com/Otitodev/seskit). Recipients are the sender's own users. "
        "Bounce and complaint notifications are delivered by SNS to this installation "
        "and the addresses are suppressed automatically; every message carries a "
        f"one-click List-Unsubscribe header. {readiness.delivered_count} message(s) "
        "have been sent and confirmed delivered through this pipeline in the sandbox."
    )


def _contacts(addresses: list[str]) -> tuple[str, ...]:
    """Trimmed, de-duplicated, at least one and at most AWS's four."""
    seen: list[str] = []
    for raw in addresses:
        address = raw.strip()
        if address and address not in seen:
            seen.append(address)
    if not seen:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "At least one contact email address is required. AWS writes to it about the review.",
        )
    if len(seen) > MAX_CONTACTS:
        raise APIError(
            ErrorType.INVALID_REQUEST, f"AWS accepts at most {MAX_CONTACTS} contact addresses."
        )
    for address in seen:
        if "@" not in address or " " in address:
            raise APIError(ErrorType.INVALID_REQUEST, f"{address!r} is not an email address.")
    return tuple(seen)


def _require_url(url: str) -> None:
    """An absolute http(s) URL. AWS wants a site it can look at."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or "." not in parts.netloc:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "The website URL must be a full address, such as https://example.com.",
        )
