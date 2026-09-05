"""Recording a provider notification (§15).

One function, reached by both transports. SQS polling and the HTTPS receiver
differ only in how a notification arrives; what happens to it afterwards is
written and tested once here.

The return value says what the caller should do with the message it just
handled, which is the part that matters to a queue:

    RECORDED        - done, remove it
    DUPLICATE       - already had it, remove it
    UNKNOWN_MESSAGE - not ours, remove it; retrying will never help
    IGNORED         - a type we do not model, remove it and log

There is deliberately no "retry" outcome. A transient failure *raises*, so the
caller cannot mistake it for a decision - acknowledging something we failed to
record is how events disappear without trace.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.events.normalise import (
    UnknownEventType,
    event_name,
    occurred_at,
    parse_event_type,
    provider_message_id,
    recipients,
    summarise,
    suppression_reason,
    to_public,
)
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.logging import get_logger
from seskit_core.models import Email, EmailEvent, EventType, SuppressionReason
from seskit_core.models.base import utcnow
from seskit_core.services.suppression import suppress, suppressed_among
from seskit_core.services.webhooks import queue_deliveries

logger = get_logger(__name__)


class Outcome(StrEnum):
    """What the caller should do with the notification it just passed in."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    UNKNOWN_MESSAGE = "unknown_message"
    IGNORED = "ignored"

    @property
    def is_settled(self) -> bool:
        """Whether the notification can be removed from the queue.

        Everything here is settled. A transient failure raises instead, because
        the distinction that matters to a queue is between "I dealt with this"
        and "ask me again" - and silently acknowledging something we failed to
        record is how events disappear without trace.
        """
        return True


async def ingest_event(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    provider_event_id: str | None,
) -> tuple[Outcome, EmailEvent | None]:
    """Record one SES event, exactly once.

    ``provider_event_id`` is the SNS MessageId. It is what makes redelivery
    harmless, and it comes from the envelope rather than the event body -
    the body is identical across redeliveries, so it cannot distinguish them.
    """
    try:
        event_type = parse_event_type(payload)
    except UnknownEventType:
        # Acknowledged rather than retried. A type we do not model will not
        # become one by being delivered again, and a queue that keeps redelivering
        # it stops making progress on everything behind it.
        logger.info("event_type_unrecognised", event_name=event_name(payload))
        return Outcome.IGNORED, None

    message_id = provider_message_id(payload)
    if not message_id:
        logger.info("event_without_message_id", event_type=event_type.value)
        return Outcome.UNKNOWN_MESSAGE, None

    email = await session.scalar(select(Email).where(Email.provider_message_id == message_id))
    if email is None:
        # Usually a message sent before this instance existed, or from another
        # tool sharing the account. Nothing to attach it to, and no amount of
        # retrying will conjure the row.
        logger.info("event_for_unknown_message", event_type=event_type.value)
        return Outcome.UNKNOWN_MESSAGE, None

    if provider_event_id is not None:
        existing = await session.scalar(
            select(EmailEvent).where(EmailEvent.provider_event_id == provider_event_id)
        )
        if existing is not None:
            return Outcome.DUPLICATE, existing

    occurred = occurred_at(payload, event_type)
    event_id = generate_id(IDPrefix.EVENT)
    event = EmailEvent(
        id=event_id,
        email_id=email.id,
        event_type=event_type.value,
        provider_event_id=provider_event_id,
        occurred_at=occurred,
        payload=to_public(
            event_id=event_id,
            event_type=event_type,
            email_id=email.id,
            occurred=occurred,
            data=summarise(payload, event_type),
        ),
    )
    session.add(event)

    apply_to_email(email, event_type, payload)

    try:
        await session.flush()
    except IntegrityError:
        # Two deliveries of the same notification racing each other. The unique
        # constraint decided; this is the loser, and it reports a duplicate
        # rather than an error, because from the caller's view nothing is wrong.
        await session.rollback()
        existing = await session.scalar(
            select(EmailEvent).where(EmailEvent.provider_event_id == provider_event_id)
        )
        return Outcome.DUPLICATE, existing

    # The Phase 8 seam. Queueing is durable - a row per enabled endpoint - so a
    # webhook survives the process dying between here and the delivery attempt.
    # Deliberately after the flush: a delivery pointing at an event that was
    # never committed would be one nothing can ever send.
    await queue_deliveries(session, event)

    # The Phase 11 seam, after the cause has been queued so that the bounce is
    # attempted before the suppression it produced. Best effort only: deliveries
    # retry independently and per endpoint, so webhook *arrival* order is not
    # something SESKit can promise and applications must not rely on it.
    #
    # Both land in the same transaction either way, so no observer ever sees the
    # suppression without the bounce that explains it.
    await _apply_suppression(session, email=email, event=event, payload=payload)

    logger.info(
        "event_recorded",
        event_id=event.id,
        email_id=email.id,
        event_type=event_type.value,
    )
    return Outcome.RECORDED, event


async def _apply_suppression(
    session: AsyncSession,
    *,
    email: Email,
    event: EmailEvent,
    payload: dict[str, Any],
) -> None:
    """Add the addresses this event condemns to the project's list.

    Only the recipients the event actually names, which for a bounce is not
    necessarily everyone the message went to - suppressing the whole
    destination list because one address is dead would take out colleagues who
    received it perfectly well.

    Scoped to the message's own project. That is the point of SESKit holding
    this list rather than SES, whose equivalent is account-wide.
    """
    event_type = EventType(event.event_type)
    reason = suppression_reason(payload, event_type)
    if reason is None:
        return

    named = recipients(payload, event_type)
    # Asked before writing, so the event below reports what actually changed.
    # An address suppressed last week bouncing again is not news, and an
    # integration that received `email.suppressed` for it twice would have to
    # deduplicate something SESKit already knows.
    already = await suppressed_among(session, project_id=email.project_id, addresses=named)

    added: list[str] = []
    for address in named:
        row = await suppress(
            session,
            project_id=email.project_id,
            address=address,
            reason=reason,
            source_event_id=event.id,
        )
        if row.address not in already:
            added.append(row.address)

    if added:
        await _record_suppression_event(
            session, email=email, cause=event, addresses=added, reason=reason
        )


async def _record_suppression_event(
    session: AsyncSession,
    *,
    email: Email,
    cause: EmailEvent,
    addresses: list[str],
    reason: SuppressionReason,
) -> None:
    """Tell the customer's application what SESKit just decided.

    One event for the addresses this notification condemned, rather than one
    per address: they share a cause, and a bounce naming three dead mailboxes
    is one thing that happened.

    ``provider_event_id`` is null because no provider sent this - SESKit did.
    Nulls do not collide in a unique index, so that costs no deduplication that
    was ever available here.

    ``occurred_at`` is now rather than the cause's timestamp. SESKit suppressed
    the address when it processed the notification, and a bounce that sat in a
    queue for an hour did not suppress anything an hour ago.
    """
    event_id = generate_id(IDPrefix.EVENT)
    occurred = utcnow()

    event = EmailEvent(
        id=event_id,
        email_id=email.id,
        event_type=EventType.SUPPRESSED.value,
        provider_event_id=None,
        occurred_at=occurred,
        payload=to_public(
            event_id=event_id,
            event_type=EventType.SUPPRESSED,
            email_id=email.id,
            occurred=occurred,
            data={"to": addresses, "reason": reason.value, "caused_by": cause.id},
        ),
    )
    session.add(event)
    await session.flush()

    await queue_deliveries(session, event)
    logger.info(
        "suppression_reported",
        event_id=event.id,
        caused_by=cause.id,
        count=len(addresses),
    )


def apply_to_email(email: Email, event_type: EventType, payload: dict[str, Any]) -> None:
    """Update the message from what the event says.

    Only ``delivered`` writes anything. A bounce deliberately does not rewrite
    ``status``: the message *was* sent, and then it bounced. Both are true, and
    collapsing them into one field loses the first - which is the one that says
    whether SESKit did its job.
    """
    if event_type is EventType.DELIVERED and email.delivered_at is None:
        email.delivered_at = occurred_at(payload, event_type)
