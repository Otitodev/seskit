"""Choosing who carries a message, and accepting it for sending (§8, §11, §25).

This returns a provider **name**, never an adapter. Core must not import a
provider (§32.8), and the API and the worker each map the name onto a concrete
one - which is also what lets a test substitute a fake without the service layer
knowing that happened.

The rule, with the edge that matters spelled out:

    no AWS connection            -> SMTP. Local development works immediately.
    connected + sender verified  -> SES
    connected + sender NOT       -> refuse: domain_not_verified
    neither configured           -> refuse, saying what to set

The third line is the one worth being deliberate about. Once a project has
connected AWS it has declared an intent to send for real; quietly falling back
to a local mailbox would report success while delivering to nobody. §19 has
``domain_not_verified`` for exactly this.
"""

from __future__ import annotations

from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.errors import APIError, ErrorType
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailAttachment,
    EmailProvider,
    EmailStatus,
    Identity,
)
from seskit_core.providers.types import IdentityType, OutboundEmail, VerificationStatus

NO_PROVIDER_MESSAGE = (
    "This project cannot send yet. Connect an AWS account, or set SMTP_HOST to deliver locally."
)


def _domain_of(address: str) -> str:
    _, addr = parseaddr(address)
    return addr.rpartition("@")[2].lower()


def _bare(address: str) -> str:
    _, addr = parseaddr(address)
    return addr.lower()


async def sender_is_verified(session: AsyncSession, *, project_id: str, sender: str) -> bool:
    """Whether SES will accept this address as a ``From:``.

    Two ways to qualify: the exact address is a verified email identity, or its
    domain is a verified domain identity. The second is why a verified domain
    lets a project send as anything on it.
    """
    address, domain = _bare(sender), _domain_of(sender)
    if not domain:
        return False

    rows = await session.scalars(
        select(Identity).where(
            Identity.project_id == project_id,
            Identity.verification_status == VerificationStatus.SUCCESS.value,
        )
    )
    for identity in rows:
        if identity.type is IdentityType.EMAIL_ADDRESS and identity.value.lower() == address:
            return True
        if identity.type is IdentityType.DOMAIN and identity.value.lower() == domain:
            return True
    return False


async def choose_provider(
    session: AsyncSession,
    *,
    project_id: str,
    sender: str,
    smtp_configured: bool,
) -> EmailProvider:
    """Decide which backend carries this message, or refuse and say why."""
    connection = await session.scalar(
        select(AWSConnection).where(AWSConnection.project_id == project_id)
    )
    connected = connection is not None and connection.status == ConnectionStatus.CONNECTED.value

    if connected:
        if await sender_is_verified(session, project_id=project_id, sender=sender):
            return EmailProvider.SES
        raise APIError(
            ErrorType.DOMAIN_NOT_VERIFIED,
            f"{_bare(sender) or sender!r} is not a verified sender for this project. "
            f"Verify the address or its domain on the Domains page before sending.",
        )

    if smtp_configured:
        return EmailProvider.SMTP

    raise APIError(ErrorType.INVALID_REQUEST, NO_PROVIDER_MESSAGE)


async def configuration_set_for(
    session: AsyncSession, *, project_id: str, provider: EmailProvider
) -> str | None:
    """Which SES configuration set this message should be sent through (§15).

    ``None`` for SMTP, which has no such concept, and ``None`` for a project
    that has not set events up. Resolved once, at accept time, and stored on the
    row: a message must always report the set it was actually sent through, and
    looking it up again at send time would answer for the project's *current*
    setup rather than the one in force when the message was accepted.
    """
    if provider is not EmailProvider.SES:
        return None

    connection = await session.scalar(
        select(AWSConnection).where(AWSConnection.project_id == project_id)
    )
    return connection.configuration_set if connection else None


def to_outbound(email: Email) -> OutboundEmail:
    """The stored row as the vocabulary a provider speaks.

    Attachments come off the relationship, which is ``selectin`` loaded - the
    worker reads the row and gets the content with it, rather than the API
    having had to carry megabytes through the queue.
    """
    from seskit_core.providers.types import Attachment

    return OutboundEmail(
        sender=email.from_address,
        to=list(email.to_addresses),
        subject=email.subject,
        html=email.html_body,
        text=email.text_body,
        cc=list(email.cc_addresses),
        bcc=list(email.bcc_addresses),
        reply_to=list(email.reply_to),
        attachments=[
            Attachment(
                filename=item.filename,
                content=item.content,
                content_type=item.content_type,
            )
            for item in email.attachments
        ],
        # Recorded on the row at accept time rather than looked up now, so a
        # message always reports the configuration set it was actually sent
        # through - even after the project's events are torn down and set up
        # again under a different name.
        configuration_set=email.configuration_set,
    )


async def find_by_idempotency_key(
    session: AsyncSession, *, project_id: str, key: str
) -> Email | None:
    email: Email | None = await session.scalar(
        select(Email).where(Email.project_id == project_id, Email.idempotency_key == key)
    )
    return email


def record_sent(email: Email, *, provider: EmailProvider, provider_message_id: str) -> None:
    """Mark a message as accepted by its provider.

    ``sent`` means a provider took it, which is the last thing SESKit can
    observe on its own. Whether it arrived is a delivery event, and that is
    Phase 7 - which is why ``delivered_at`` is untouched here.
    """
    from seskit_core.models import utcnow

    email.status = EmailStatus.SENT.value
    email.provider = provider.value
    email.provider_message_id = provider_message_id
    email.sent_at = utcnow()
    email.last_error = None


def record_failure(email: Email, error: APIError) -> None:
    """Mark a message as failed, with a message safe to show a customer."""
    email.status = EmailStatus.FAILED.value
    email.last_error = error.message


#: Failures worth another attempt. Everything else is terminal: a rejected
#: message rejected again is just a second rejection, and retrying an
#: authorisation failure will not grant the permission.
RETRYABLE_ERRORS = frozenset({ErrorType.PROVIDER_ERROR})


def is_retryable(error: APIError) -> bool:
    return error.error_type in RETRYABLE_ERRORS


def attachment_rows(
    attachments: list[tuple[str, str, bytes]],
) -> list[EmailAttachment]:
    """Build attachment rows from (filename, content_type, content) triples."""
    return [
        EmailAttachment(
            filename=filename,
            content_type=content_type,
            content=content,
            size_bytes=len(content),
        )
        for filename, content_type, content in attachments
    ]
