"""Draining delivery events off the queue (§15).

What is asserted here is mostly about *acknowledgement*, because that is where
this kind of code goes wrong quietly. A message deleted too early is a delivery
event nobody will ever see again; a message never deleted is a queue that stops
making progress. `docs/design/prior-art.md` records a comparable project that answered
success on parse failures, which is the first mistake.

The queue is a fake rather than moto because what matters is which messages
were deleted and which were left - and a fake can be asked that directly,
where moto would only let it be inferred.
"""

from __future__ import annotations

import json

import pytest
from fakes import ses_events
from seskit_core.models import Email, EmailEvent, EmailStatus, EventType
from seskit_core.providers import QueuedNotification
from seskit_core.services import create_project, register_user
from seskit_worker import events as worker_events
from seskit_worker.events import drain, handle
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct-horse-battery"


class FakeNotificationQueue:
    """Messages in, receipts out. Records exactly what was acknowledged."""

    def __init__(self, *bodies: str, batch_size: int = 10) -> None:
        self.pending = [
            QueuedNotification(receipt=f"receipt-{index}", body=body, queue_message_id=f"q-{index}")
            for index, body in enumerate(bodies)
        ]
        self.batch_size = batch_size
        self.deleted: list[str] = []
        self.receives = 0

    async def receive(
        self,
        *,
        max_messages: int = 10,
        wait_seconds: int = 20,
        visibility_timeout: int = 60,
    ) -> list[QueuedNotification]:
        self.receives += 1
        batch, self.pending = self.pending[: self.batch_size], self.pending[self.batch_size :]
        return batch

    async def delete(self, notification: QueuedNotification) -> None:
        self.deleted.append(notification.receipt)


def _envelope(payload: dict[str, object], *, message_id: str = "sns-1") -> str:
    return json.dumps(ses_events.sns_envelope(json.dumps(payload), message_id=message_id))


async def _sent_email(session: AsyncSession, *, email: str = "owner@example.com") -> Email:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    row = Email(
        project_id=project.id,
        from_address=ses_events.SENDER,
        to_addresses=[ses_events.RECIPIENT],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hello",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id=ses_events.MESSAGE_ID,
    )
    session.add(row)
    await session.commit()
    return row


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(EmailEvent)) or 0)


# ------------------------------------------------------------------ recording ---


async def test_a_delivery_is_recorded_and_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _sent_email(db_session)
    queue = FakeNotificationQueue(_envelope(ses_events.delivery()))

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=5,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 1
    assert queue.deleted == ["receipt-0"]
    assert await _count(db_session) == 1


async def test_the_envelope_id_is_what_deduplicates(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The test that matters for this transport.

    SQS issues a *new* message id for each delivery of the same notification, so
    keying on that would deduplicate nothing. The SNS envelope's id is stable
    across redeliveries, which is why it is the one that is used - and why raw
    message delivery, which strips the envelope, stays off.
    """
    await _sent_email(db_session)
    body = _envelope(ses_events.bounce(), message_id="sns-same")
    # Two SQS deliveries of one notification: different receipts, same envelope.
    queue = FakeNotificationQueue(body, body)

    await drain(
        queue,
        session_factory=session_factory,
        max_batches=5,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert await _count(db_session) == 1
    # Both are acknowledged: the duplicate is settled, not left to come back.
    assert queue.deleted == ["receipt-0", "receipt-1"]


async def test_several_events_in_one_batch_all_land(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _sent_email(db_session)
    queue = FakeNotificationQueue(
        _envelope(ses_events.opened(), message_id="sns-a"),
        _envelope(ses_events.clicked(), message_id="sns-b"),
    )

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=5,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 2
    assert await _count(db_session) == 2


async def test_batches_are_drained_until_the_queue_is_empty(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _sent_email(db_session)
    queue = FakeNotificationQueue(
        _envelope(ses_events.delivery(), message_id="sns-a"),
        _envelope(ses_events.opened(), message_id="sns-b"),
        _envelope(ses_events.clicked(), message_id="sns-c"),
        batch_size=1,
    )

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=10,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 3
    # Three batches plus the empty one that ended the loop.
    assert queue.receives == 4


async def test_a_pass_is_bounded(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A backlog must not monopolise the worker. The job queued behind this one
    is a send, and a user notices a late email long before a late receipt.
    """
    await _sent_email(db_session)
    queue = FakeNotificationQueue(
        *[_envelope(ses_events.delivery(), message_id=f"sns-{i}") for i in range(6)],
        batch_size=1,
    )

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 2
    assert queue.receives == 2
    assert len(queue.pending) == 4


async def test_an_empty_queue_is_asked_once(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Long polling already waited. Asking again immediately would bill for the
    same answer.
    """
    queue = FakeNotificationQueue()

    assert (
        await drain(
            queue,
            session_factory=session_factory,
            max_batches=10,
            wait_seconds=0,
            visibility_timeout=30,
        )
        == 0
    )
    assert queue.receives == 1


# ------------------------------------------------------------ acknowledgement ---


async def test_an_unreadable_body_is_acknowledged_not_retried(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Redelivering something that is not JSON will not make it JSON."""
    queue = FakeNotificationQueue("this is not json")

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 0
    assert queue.deleted == ["receipt-0"]


async def test_a_subscription_confirmation_is_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """SNS confirms SQS subscriptions itself, so this is unexpected here -
    but leaving it on the queue forever would block everything behind it.
    """
    queue = FakeNotificationQueue(
        json.dumps({"Type": "SubscriptionConfirmation", "MessageId": "c", "SubscribeURL": "x"})
    )

    await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert queue.deleted == ["receipt-0"]


async def test_an_event_for_an_unknown_message_is_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No Email row to attach it to - usually a message sent before this
    instance existed. Retrying will never conjure the row.
    """
    queue = FakeNotificationQueue(_envelope(ses_events.delivery()))

    recorded = await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert recorded == 0
    assert queue.deleted == ["receipt-0"]
    assert await _count(db_session) == 0


async def test_an_unknown_event_type_is_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AWS adds event types. A new one must not wedge the queue."""
    await _sent_email(db_session)
    queue = FakeNotificationQueue(
        _envelope({"eventType": "SomethingNew", "mail": {"messageId": ses_events.MESSAGE_ID}})
    )

    await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    assert queue.deleted == ["receipt-0"]
    assert await _count(db_session) == 0


async def test_a_failure_leaves_the_message_on_the_queue(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetry the whole design rests on.

    An exception means we do not know what happened. Acknowledging then is how
    events disappear without trace - which is the failure docs/design/prior-art.md
    records, where a transient bug answered success and dropped the event.
    """
    await _sent_email(db_session)
    queue = FakeNotificationQueue(_envelope(ses_events.delivery()))

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(worker_events, "ingest_event", explode)

    with pytest.raises(RuntimeError):
        await handle(queue, queue.pending[0], session_factory=session_factory)

    # Left on the queue, so the visibility timeout brings it back.
    assert queue.deleted == []


# ------------------------------------------------------------------ effects ---


async def test_the_email_learns_it_was_delivered(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The dashboard has shown a dash there since Phase 6."""
    email = await _sent_email(db_session)
    queue = FakeNotificationQueue(_envelope(ses_events.delivery()))

    await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    await db_session.refresh(email)
    assert email.delivered_at is not None


async def test_a_bounce_is_recorded_without_rewriting_the_send(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    email = await _sent_email(db_session)
    queue = FakeNotificationQueue(_envelope(ses_events.bounce()))

    await drain(
        queue,
        session_factory=session_factory,
        max_batches=2,
        wait_seconds=0,
        visibility_timeout=30,
    )

    await db_session.refresh(email)
    event = await db_session.scalar(select(EmailEvent))
    assert event is not None
    assert event.type is EventType.BOUNCED
    assert email.status == EmailStatus.SENT.value
    assert email.delivered_at is None
