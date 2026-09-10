"""Accepting a message for sending (§11, §14).

Everything between "a caller asked us to send this" and "it is on the queue":
the suppression check, choosing a provider, assembling the message to find out
whether it is even sendable, the row, and the configuration set the worker will
need. Validate here, send elsewhere.

This lived inside the ``POST /v1/emails`` handler until the dashboard grew a
send form. Two callers is the reason it moved, but not the reason it is worth
moving: a form that re-implemented any of this would be testing the form. The
whole value of a "send a test message" button is that it exercises the same
path a customer's application does, and the only way to be sure of that is for
there to be one path.

What stayed in the route is what is genuinely HTTP: parsing a request body,
idempotency keys, and the response shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.email import assert_within_size
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import Email, EmailStatus
from seskit_core.providers.types import Attachment, OutboundEmail
from seskit_core.services.sending import (
    attachment_rows,
    choose_provider,
    configuration_set_for,
)
from seskit_core.services.suppression import suppressed_among


@dataclass(frozen=True, slots=True)
class Outgoing:
    """What a caller asked us to send, in SESKit's own terms.

    Deliberately not the API's request model. Core must not import the API
    package, and a dashboard form should not have to build a
    ``SendEmailRequest`` - with an idempotency key it has no use for - to say
    "send this".
    """

    sender: str
    to: list[str]
    subject: str
    html: str | None = None
    text: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    #: ``(filename, content_type, content)``, already decoded.
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)


async def refuse_suppressed(session: AsyncSession, *, project_id: str, message: Outgoing) -> None:
    """Stop a message aimed at an address this project has suppressed.

    **Fails the whole request**, not the suppressed recipients. Sending to the
    rest would need a second response shape saying who was dropped, and a
    caller who did not read it would believe everyone got the message. §31 asks
    for closed rather than partial, and refusing is the answer a retry loop can
    act on.

    Bcc is checked too. A suppressed address is suppressed however it was
    reached, and a blind copy is still a send.

    Before `choose_provider` deliberately: a project with no AWS connection is
    told what SESKit already knows about its own list rather than being sent
    away to configure sending first.
    """
    blocked = await suppressed_among(
        session,
        project_id=project_id,
        addresses=[*message.to, *message.cc, *message.bcc],
    )
    if not blocked:
        return

    ordered = sorted(blocked)
    if len(ordered) == 1:
        named, verb, pronoun = ordered[0], "is", "it"
    else:
        named = f"{', '.join(ordered[:-1])} and {ordered[-1]}"
        verb, pronoun = "are", "them"

    raise APIError(
        ErrorType.SUPPRESSED_RECIPIENT,
        f"{named} {verb} on this project's suppression list, so nothing was sent. "
        "An address lands there after a hard bounce or a complaint, when the "
        "recipient unsubscribes, or by hand. The Suppressions page says which, "
        f"and can take {pronoun} off if you believe mail can be delivered there again.",
    )


async def accept_email(
    session: AsyncSession,
    *,
    project_id: str,
    message: Outgoing,
    smtp_configured: bool,
    max_message_bytes: int,
    idempotency_key: str | None = None,
) -> Email:
    """Record a message as queued, or raise an `APIError` saying why not.

    Does not commit and does not enqueue. Both belong to the caller: the API
    route has to answer an idempotency collision by returning the winner's row,
    which it can only do once it has seen the `IntegrityError`, and the job must
    not be enqueued before the row it names is committed.
    """
    await refuse_suppressed(session, project_id=project_id, message=message)

    provider = await choose_provider(
        session,
        project_id=project_id,
        sender=message.sender,
        smtp_configured=smtp_configured,
    )

    # Assembled once here purely to validate: it is what catches a malformed
    # address, an injected header and an oversized message, and doing it now
    # means those come back to the caller rather than surfacing in a worker log
    # an hour later.
    outbound = OutboundEmail(
        sender=message.sender,
        to=message.to,
        subject=message.subject,
        html=message.html,
        text=message.text,
        cc=message.cc,
        bcc=message.bcc,
        reply_to=message.reply_to,
        headers=message.headers,
        attachments=[
            Attachment(filename=name, content=content, content_type=content_type)
            for name, content_type, content in message.attachments
        ],
    )
    assert_within_size(outbound, max_bytes=max_message_bytes)

    email = Email(
        project_id=project_id,
        from_address=message.sender,
        to_addresses=message.to,
        cc_addresses=message.cc,
        bcc_addresses=message.bcc,
        reply_to=message.reply_to,
        subject=message.subject,
        html_body=message.html,
        text_body=message.text,
        # Stored, not just validated above. The worker assembles the message
        # from this row, so a header that does not reach the row is a header
        # the caller was told we would send and we did not.
        headers=message.headers,
        status=EmailStatus.QUEUED.value,
        idempotency_key=idempotency_key,
        provider=provider.value,
        # Without this SES publishes no events for the message and its delivery
        # history stays permanently empty - so it is settled here, where the
        # project's setup is known, rather than in the worker.
        configuration_set=await configuration_set_for(
            session, project_id=project_id, provider=provider
        ),
    )
    email.attachments.extend(attachment_rows(message.attachments))
    session.add(email)
    return email


__all__ = ["Outgoing", "accept_email", "refuse_suppressed"]
