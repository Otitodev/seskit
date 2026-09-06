"""Events SESKit raises about itself (§15, §31 Phase 11).

`ingest.py` records what a provider told us. This records what SESKit decided,
which is a different thing and now happens for more than one reason - a hard
bounce, a complaint, and a recipient pressing Unsubscribe all end with an
address on the list, and an application reconciling its own mailing list
against SESKit's has to hear about all three.

It lives under ``events`` rather than beside the suppression service so the
dependency runs one way: events know about services, services do not know about
events.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.events.normalise import to_public
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.logging import get_logger
from seskit_core.models import EmailEvent, EventType, SuppressionReason
from seskit_core.models.base import utcnow
from seskit_core.services.webhooks import queue_deliveries

logger = get_logger(__name__)


async def record_suppression_event(
    session: AsyncSession,
    *,
    email_id: str,
    addresses: list[str],
    reason: SuppressionReason,
    caused_by: str | None = None,
) -> EmailEvent:
    """Tell the customer's application that an address has been suppressed.

    One event for the addresses one cause condemned, rather than one per
    address: a bounce naming three dead mailboxes is one thing that happened.

    ``provider_event_id`` is null because no provider sent this - SESKit did.
    Nulls do not collide in a unique index, so that costs no deduplication that
    was ever available here.

    ``occurred_at`` is now rather than the cause's timestamp. SESKit suppressed
    the address when it processed the cause, and a bounce that sat in a queue
    for an hour did not suppress anything an hour ago.

    ``caused_by`` is the event that led to this, and is null when there was
    none - an unsubscribe is the recipient telling SESKit directly. The key is
    always present so a consumer can read it without checking first.
    """
    event_id = generate_id(IDPrefix.EVENT)
    occurred = utcnow()

    event = EmailEvent(
        id=event_id,
        email_id=email_id,
        event_type=EventType.SUPPRESSED.value,
        provider_event_id=None,
        occurred_at=occurred,
        payload=to_public(
            event_id=event_id,
            event_type=EventType.SUPPRESSED,
            email_id=email_id,
            occurred=occurred,
            data={"to": addresses, "reason": reason.value, "caused_by": caused_by},
        ),
    )
    session.add(event)
    await session.flush()

    await queue_deliveries(session, event)
    logger.info(
        "suppression_reported",
        event_id=event.id,
        caused_by=caused_by,
        count=len(addresses),
    )
    return event
