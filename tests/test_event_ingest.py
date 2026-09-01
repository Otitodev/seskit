"""Recording provider notifications (§15).

The deduplication test is the reason this file exists. SNS and SQS are both
at-least-once, so the same notification *will* arrive twice; without the unique
constraint a redelivered bounce becomes two bounces and every rate §18 computes
is wrong. `docs/prior-art.md` records a comparable project that threaded the
message id through its queue and then keyed on nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fakes import ses_events
from seskit_core.events import Outcome, ingest_event
from seskit_core.models import Email, EmailEvent, EmailStatus, EventType
from seskit_core.services import create_project, register_user
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"


async def _sent_email(session: AsyncSession, **overrides: object) -> Email:
    """An email already sent, with the provider message id events key on."""
    user = await register_user(
        session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(session, user_id=user.id, name="Sending")
    defaults: dict[str, object] = {
        "project_id": project.id,
        "from_address": ses_events.SENDER,
        "to_addresses": [ses_events.RECIPIENT],
        "cc_addresses": [],
        "bcc_addresses": [],
        "reply_to": [],
        "subject": "Welcome",
        "text_body": "Hello",
        "status": EmailStatus.SENT.value,
        "provider": "ses",
        "provider_message_id": ses_events.MESSAGE_ID,
    }
    defaults.update(overrides)
    email = Email(**defaults)
    session.add(email)
    await session.flush()
    return email


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(EmailEvent)) or 0)


# ---------------------------------------------------------------- recording ---


async def test_a_delivery_is_recorded_against_its_email(db_session: AsyncSession) -> None:
    email = await _sent_email(db_session)

    outcome, event = await ingest_event(
        db_session, ses_events.delivery(), provider_event_id="sns-1"
    )

    assert outcome is Outcome.RECORDED
    assert event is not None
    assert event.email_id == email.id
    assert event.type is EventType.DELIVERED


async def test_a_delivery_sets_delivered_at(db_session: AsyncSession) -> None:
    """The dashboard has shown a dash there since Phase 6."""
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.delivery(), provider_event_id="sns-1")

    assert email.delivered_at == datetime(2026, 8, 30, 9, 0, 3, tzinfo=UTC)


async def test_a_bounce_does_not_rewrite_the_send_status(db_session: AsyncSession) -> None:
    """The message *was* sent, and then it bounced. Both are true, and
    collapsing them into one field loses the half that says whether SESKit did
    its job.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-2")

    assert email.status == EmailStatus.SENT.value
    assert email.delivered_at is None


async def test_the_stored_payload_is_the_normalised_one(db_session: AsyncSession) -> None:
    """§15: provider payloads must not leak. Phase 8 sends this to customers."""
    await _sent_email(db_session)

    _, event = await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-3")

    assert event is not None
    assert event.payload["type"] == "email.bounced"
    assert "sourceArn" not in repr(event.payload)


# ------------------------------------------------------------ deduplication ---


async def test_the_same_notification_twice_records_one_event(
    db_session: AsyncSession,
) -> None:
    """The test that matters.

    SNS and SQS are at-least-once. Without this a redelivered bounce is two
    bounces, and the bounce rate a user sees is wrong in the direction that
    makes them panic.
    """
    await _sent_email(db_session)

    first, event_one = await ingest_event(
        db_session, ses_events.bounce(), provider_event_id="sns-same"
    )
    second, event_two = await ingest_event(
        db_session, ses_events.bounce(), provider_event_id="sns-same"
    )

    assert first is Outcome.RECORDED
    assert second is Outcome.DUPLICATE
    assert await _count(db_session) == 1

    # The duplicate hands back the event that already existed, so a caller can
    # act on it without having to re-query.
    assert event_one is not None
    assert event_two is not None
    assert event_two.id == event_one.id


async def test_different_notifications_both_record(db_session: AsyncSession) -> None:
    """Deduplication must not swallow genuinely distinct events - an open and a
    click for one message are two things that happened.
    """
    await _sent_email(db_session)

    await ingest_event(db_session, ses_events.opened(), provider_event_id="sns-a")
    await ingest_event(db_session, ses_events.clicked(), provider_event_id="sns-b")

    assert await _count(db_session) == 2


async def test_events_without_a_provider_id_are_not_deduplicated(
    db_session: AsyncSession,
) -> None:
    """NULLs do not collide in a unique index. A provider offering no event id
    should still be recordable; it simply gets no protection.
    """
    await _sent_email(db_session)

    await ingest_event(db_session, ses_events.opened(), provider_event_id=None)
    await ingest_event(db_session, ses_events.opened(), provider_event_id=None)

    assert await _count(db_session) == 2


# ------------------------------------------------------------- not for us ---


async def test_an_event_for_an_unknown_message_is_settled_not_retried(
    db_session: AsyncSession,
) -> None:
    """Usually a message sent before this instance existed, or by another tool
    sharing the account. No amount of retrying will conjure the row, and a queue
    that keeps trying stops making progress on everything behind it.
    """
    outcome, event = await ingest_event(
        db_session, ses_events.delivery(), provider_event_id="sns-orphan"
    )

    assert outcome is Outcome.UNKNOWN_MESSAGE
    assert outcome.is_settled is True
    assert event is None
    assert await _count(db_session) == 0


async def test_an_unrecognised_type_is_ignored_not_fatal(db_session: AsyncSession) -> None:
    """AWS adds event types. A new one must not wedge the queue."""
    await _sent_email(db_session)

    outcome, event = await ingest_event(
        db_session,
        {"eventType": "SomethingNew", "mail": {"messageId": ses_events.MESSAGE_ID}},
        provider_event_id="sns-new",
    )

    assert outcome is Outcome.IGNORED
    assert event is None
    assert await _count(db_session) == 0


async def test_a_payload_with_no_message_id_is_settled(db_session: AsyncSession) -> None:
    outcome, _ = await ingest_event(
        db_session, {"eventType": "Delivery"}, provider_event_id="sns-x"
    )

    assert outcome is Outcome.UNKNOWN_MESSAGE


# ------------------------------------------------------------- boundaries ---


async def test_an_event_lands_only_on_its_own_message(db_session: AsyncSession) -> None:
    """Correlation is by provider message id, which is unique per send. An
    event must not attach itself to a different project's mail.
    """
    mine = await _sent_email(db_session)

    stranger = await register_user(
        db_session, email="them@example.com", password=PASSWORD, allow_signup=True
    )
    other_project = await create_project(db_session, user_id=stranger.id, name="Theirs")
    other = Email(
        project_id=other_project.id,
        from_address="a@example.com",
        to_addresses=["b@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Theirs",
        text_body="x",
        status=EmailStatus.SENT.value,
        provider_message_id="a-different-message-id",
    )
    db_session.add(other)
    await db_session.flush()

    _, event = await ingest_event(db_session, ses_events.delivery(), provider_event_id="sns-1")

    assert event is not None
    assert event.email_id == mine.id


async def test_deleting_an_email_deletes_its_events(db_session: AsyncSession) -> None:
    email = await _sent_email(db_session)
    await ingest_event(db_session, ses_events.delivery(), provider_event_id="sns-1")

    await db_session.delete(email)
    await db_session.flush()

    assert await _count(db_session) == 0
