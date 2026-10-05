"""An event can belong to a message that arrived (inbound email, Phase C).

Until now every event was about a message SESKit sent, so ``email_id`` was
required. Received mail has no ``emails`` row, and the webhook machinery is keyed
to an event, so an event now has exactly one of two parents. That "exactly one" is
held by a check constraint, which only a real database can test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from seskit_core.events import to_public, to_public_received
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models import (
    EVENT_LABELS,
    PUBLIC_EVENT_TYPES,
    Email,
    EmailEvent,
    EventType,
    Project,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_inbound_models import _inbound, _project

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


async def _sent(session: AsyncSession, project: Project) -> Email:
    email = Email(
        project_id=project.id,
        from_address="from@example.com",
        to_addresses=["to@example.org"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Hello",
        text_body="Hi",
        status="sent",
        provider="ses",
    )
    session.add(email)
    await session.flush()
    return email


def _event(**parents: str | None) -> EmailEvent:
    event_id = generate_id(IDPrefix.EVENT)
    return EmailEvent(
        id=event_id,
        event_type=EventType.RECEIVED.value,
        occurred_at=NOW,
        payload={},
        **parents,
    )


# ------------------------------------------------------------------- pure ---


def test_received_is_a_public_event_with_a_label() -> None:
    assert EventType.RECEIVED in PUBLIC_EVENT_TYPES
    assert EVENT_LABELS[EventType.RECEIVED] == "Received"


def test_the_received_payload_names_the_inbound_message_not_an_email() -> None:
    """``email_id`` must never hold an inbound id: a receiver switching on the
    field's presence has to be able to trust what it refers to, and
    ``GET /v1/emails/{id}`` must never be handed one.
    """
    payload = to_public_received(
        event_id="evt_1", inbound_id="inbound_1", occurred=NOW, data={"subject": "hi"}
    )

    assert payload == {
        "id": "evt_1",
        "type": "email.received",
        "inbound_id": "inbound_1",
        "created_at": NOW.isoformat(),
        "data": {"subject": "hi"},
    }
    assert "email_id" not in payload


def test_the_sent_payload_is_unchanged_by_this() -> None:
    """The contract every existing receiver relies on."""
    payload = to_public(
        event_id="evt_1",
        event_type=EventType.DELIVERED,
        email_id="email_1",
        occurred=NOW,
        data={"to": ["a@example.org"]},
    )

    assert payload["email_id"] == "email_1"
    assert "inbound_id" not in payload
    assert payload["type"] == "email.delivered"


# --------------------------------------------------------------- database ---


async def test_a_received_event_belongs_to_the_message_that_arrived(
    db_session: AsyncSession,
) -> None:
    project = await _project(db_session)
    message = _inbound(project)
    db_session.add(message)
    await db_session.flush()
    event = _event(inbound_email_id=message.id)
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event, ["inbound_email", "email"])

    assert event.email_id is None
    assert event.email is None
    assert event.inbound_email is not None
    assert event.inbound_email.id == message.id


async def test_a_sent_event_still_belongs_to_a_sent_email(db_session: AsyncSession) -> None:
    project = await _project(db_session)
    email = await _sent(db_session, project)
    event = _event(email_id=email.id)
    event.event_type = EventType.DELIVERED.value
    db_session.add(event)

    await db_session.flush()

    assert event.inbound_email_id is None


async def test_an_event_about_nothing_is_refused(db_session: AsyncSession) -> None:
    """Neither parent would leave an event nothing can be delivered for, since
    the project is found through the parent.
    """
    db_session.add(_event())

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_event_about_two_things_is_refused(db_session: AsyncSession) -> None:
    """Both would make it ambiguous which, and the webhook would name one of
    them arbitrarily.
    """
    project = await _project(db_session)
    email = await _sent(db_session, project)
    message = _inbound(project)
    db_session.add(message)
    await db_session.flush()
    db_session.add(_event(email_id=email.id, inbound_email_id=message.id))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_a_received_message_deletes_its_events(db_session: AsyncSession) -> None:
    project = await _project(db_session)
    message = _inbound(project)
    db_session.add(message)
    await db_session.flush()
    db_session.add(_event(inbound_email_id=message.id))
    await db_session.flush()

    await db_session.delete(message)
    await db_session.flush()

    total = await db_session.scalar(select(func.count()).select_from(EmailEvent))
    assert total == 0
