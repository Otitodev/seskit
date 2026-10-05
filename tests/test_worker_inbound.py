"""Draining received mail off the queue.

As with the event poller, what is asserted is mostly about *acknowledgement*,
because that is where this kind of code goes wrong quietly. A message deleted too
early is mail nobody will ever see; one never deleted is a queue that stops
making progress.

The case that is new, and has a test to itself, is isolation. The step that can
fail here is a download, and the commonest cause is a missing permission, which
fails the same way on every message. If one failure ended the batch, the
unreadable message at the front would stall everything behind it.

The queue and the store are fakes rather than moto, because what matters is which
announcements were deleted and which objects were fetched - and a fake can be asked
that directly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fakes import ses_events
from fakes.ses import connect_project
from seskit_core.errors import APIError, ErrorType
from seskit_core.events import record_received as real_record_received
from seskit_core.models import (
    AWSConnection,
    Identity,
    InboundEmail,
    WebhookDelivery,
    WebhookEndpoint,
)
from seskit_core.providers import AWSCredentials, InboundInfrastructure, QueuedNotification
from seskit_core.services import create_project, register_user
from seskit_worker.inbound import drain_inbound, handle_inbound, poll_inbound
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_inbound_ingest import BUCKET, RAW, notification

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


class FakeStore:
    """Stored messages by key, and a record of every one fetched."""

    def __init__(self, *keys: str, error: APIError | None = None) -> None:
        self.objects = dict.fromkeys(keys, RAW)
        self.error = error
        self.fetches: list[str] = []

    async def fetch_message(self, *, bucket: str, key: str) -> bytes:
        self.fetches.append(key)
        if self.error is not None:
            raise self.error
        if key not in self.objects:
            raise APIError(ErrorType.NOT_FOUND, "gone")
        return self.objects[key]


def _body(n: int = 1, *, sns_id: str | None = None, **receipt: Any) -> str:
    """One announcement, as SQS hands it over: an SNS envelope around the notice."""
    payload = notification()
    payload["receipt"]["action"]["objectKey"] = f"seskit-x/{n}"
    payload["mail"]["messageId"] = f"ses-msg-{n}"
    payload["receipt"].update(receipt)
    return json.dumps(ses_events.sns_envelope(json.dumps(payload), message_id=sns_id or f"sns-{n}"))


async def _receiving(
    session: AsyncSession,
    email: str,
    *,
    region: str = "us-east-1",
    bucket: str = BUCKET,
    domain: str = "example.com",
) -> tuple[AWSConnection, Identity]:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=f"P-{email}")
    connection: AWSConnection = await connect_project(session, project.id, region=region)
    connection.record_inbound_infrastructure(
        InboundInfrastructure(
            bucket=bucket,
            topic_arn=f"arn:aws:sns:{region}:123456789012:seskit-inbound",
            queue_url=f"https://sqs.{region}.amazonaws.com/123456789012/seskit-inbound",
            queue_arn=f"arn:aws:sqs:{region}:123456789012:seskit-inbound",
            subscription_arn=f"arn:aws:sns:{region}:123456789012:seskit-inbound:sub",
        )
    )
    identity = Identity(
        project_id=project.id,
        identity_type="domain",
        value=domain,
        region=region,
        verification_status="success",
        inbound_rule_name="seskit-example-com",
        inbound_rule_set="seskit-inbound",
    )
    session.add(identity)
    await session.flush()
    return connection, identity


async def _drain(
    queue: FakeNotificationQueue,
    store: FakeStore,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_ids: set[str],
    bucket: str = BUCKET,
    max_batches: int = 5,
    enqueue: Any = None,
) -> int:
    return await drain_inbound(
        queue,
        store,
        bucket=bucket,
        session_factory=session_factory,
        project_ids=project_ids,
        retention_days=30,
        max_batches=max_batches,
        wait_seconds=0,
        visibility_timeout=30,
        enqueue=enqueue,
    )


async def _stored(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(InboundEmail)) or 0)


# -------------------------------------------------------------- recording ---


async def test_a_message_is_fetched_recorded_and_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    store = FakeStore("seskit-x/1")

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 1
    assert queue.deleted == ["receipt-0"]
    assert store.fetches == ["seskit-x/1"]
    row = await db_session.scalar(select(InboundEmail))
    assert row is not None
    assert row.subject == "Hello there"
    assert row.project_id == connection.project_id


async def test_a_redelivery_is_acknowledged_without_a_second_download(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The point of checking before fetching. A redelivery of a 40 MB message
    should cost a database lookup, not a transfer.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    body = _body(1, sns_id="sns-same")
    queue = FakeNotificationQueue(body, body)
    store = FakeStore("seskit-x/1")

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 1
    assert store.fetches == ["seskit-x/1"]
    assert queue.deleted == ["receipt-0", "receipt-1"]
    assert await _stored(db_session) == 1


async def test_the_same_object_announced_under_a_new_id_is_not_downloaded_again(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1, sns_id="sns-a"), _body(1, sns_id="sns-b"))
    store = FakeStore("seskit-x/1")

    await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert store.fetches == ["seskit-x/1"]
    assert queue.deleted == ["receipt-0", "receipt-1"]
    assert await _stored(db_session) == 1


# --------------------------------------------------------- acknowledgement ---


async def test_an_expired_message_is_acknowledged_not_retried(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """It will not come back, and a queue that keeps redelivering it makes no
    progress on the rest.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))

    recorded = await _drain(
        queue, FakeStore(), session_factory, project_ids={connection.project_id}
    )

    assert recorded == 0
    assert queue.deleted == ["receipt-0"]
    assert await _stored(db_session) == 0


@pytest.mark.parametrize("error_type", [ErrorType.AUTHORIZATION_FAILED, ErrorType.PROVIDER_ERROR])
async def test_a_download_that_may_succeed_later_is_left_on_the_queue(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    error_type: ErrorType,
) -> None:
    """A permission can be added and a network can come back. Deleting this would
    turn a fixable setup mistake into mail that is gone.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    store = FakeStore("seskit-x/1", error=APIError(error_type, "no"))

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 0
    assert queue.deleted == []
    assert await _stored(db_session) == 0


async def test_one_failing_message_does_not_stall_the_ones_behind_it(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The isolation case. A missing permission fails every download the same
    way, so if one failure ended the batch the unreadable message at the front
    would stall everything behind it, for ever.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1), _body(2), _body(3))

    class SelectiveStore(FakeStore):
        async def fetch_message(self, *, bucket: str, key: str) -> bytes:
            if key == "seskit-x/1":
                self.fetches.append(key)
                raise APIError(ErrorType.AUTHORIZATION_FAILED, "no")
            return await super().fetch_message(bucket=bucket, key=key)

    store = SelectiveStore("seskit-x/2", "seskit-x/3")

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 2
    # The failed one stays; the two behind it were handled.
    assert queue.deleted == ["receipt-1", "receipt-2"]
    assert await _stored(db_session) == 2


async def test_a_failure_while_recording_leaves_the_message_for_the_next_pass(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the database went away")
        return await real_record_received(*args, **kwargs)

    monkeypatch.setattr("seskit_worker.inbound.record_received", flaky)
    queue = FakeNotificationQueue(_body(1), _body(2))
    store = FakeStore("seskit-x/1", "seskit-x/2")

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 1
    assert queue.deleted == ["receipt-1"]


async def test_a_message_for_a_domain_this_queue_does_not_speak_for_is_dropped_unread(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The lock. It is dropped before anything is downloaded, so a forged
    announcement costs nothing and records nothing.
    """
    await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    store = FakeStore("seskit-x/1")

    recorded = await _drain(queue, store, session_factory, project_ids={"proj_somebody_else"})

    assert recorded == 0
    assert queue.deleted == ["receipt-0"]
    assert store.fetches == []
    assert await _stored(db_session) == 0


async def test_an_announcement_from_a_bucket_that_is_not_this_queues_is_dropped_unread(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    store = FakeStore("seskit-x/1")

    await _drain(
        queue,
        store,
        session_factory,
        project_ids={connection.project_id},
        bucket="a-different-bucket",
    )

    assert queue.deleted == ["receipt-0"]
    assert store.fetches == []


@pytest.mark.parametrize("action", ["SNS", "Lambda", "Bounce"])
async def test_another_actions_notification_is_acknowledged_without_a_fetch(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    action: str,
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1, action={"type": action}))
    store = FakeStore("seskit-x/1")

    await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert queue.deleted == ["receipt-0"]
    assert store.fetches == []


async def test_something_that_is_not_mail_is_acknowledged(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A delivery event wrongly on this queue, a body that is not JSON, and a
    subscription handshake. None can be made into mail by redelivery.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(
        json.dumps(ses_events.sns_envelope(json.dumps(ses_events.delivery()))),
        "this is not json",
        json.dumps({"Type": "SubscriptionConfirmation", "MessageId": "c", "SubscribeURL": "x"}),
    )
    store = FakeStore()

    recorded = await _drain(queue, store, session_factory, project_ids={connection.project_id})

    assert recorded == 0
    assert queue.deleted == ["receipt-0", "receipt-1", "receipt-2"]
    assert store.fetches == []


# --------------------------------------------------------------- the pass ---


async def test_a_pass_is_bounded(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A backlog must not monopolise the worker. The job queued behind this one is
    a send, and a user notices a late email long before a late message.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(*[_body(n) for n in range(6)], batch_size=1)
    store = FakeStore(*[f"seskit-x/{n}" for n in range(6)])

    recorded = await _drain(
        queue, store, session_factory, project_ids={connection.project_id}, max_batches=2
    )

    assert recorded == 2
    assert queue.receives == 2
    assert len(queue.pending) == 4


async def test_an_empty_queue_is_asked_once(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    queue = FakeNotificationQueue()

    assert await _drain(queue, FakeStore(), session_factory, project_ids={"proj_x"}) == 0
    assert queue.receives == 1


async def test_handle_reports_whether_a_message_was_recorded(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    notification_ = queue.pending[0]

    handled = await handle_inbound(
        queue,
        FakeStore("seskit-x/1"),
        notification_,
        bucket=BUCKET,
        session_factory=session_factory,
        project_ids={connection.project_id},
        retention_days=30,
    )

    assert handled is True


# ------------------------------------------------------------ the job itself ---


async def test_the_job_reads_every_queue_it_has(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _receiving(db_session, "a@example.com")
    await db_session.commit()
    queue = FakeNotificationQueue(_body(1))
    store = FakeStore("seskit-x/1")
    built: list[tuple[str, str]] = []

    def build(region: str, queue_url: str, credentials: AWSCredentials) -> FakeNotificationQueue:
        built.append((region, queue_url))
        return queue

    recorded = await poll_inbound(
        {},
        build=build,
        store_builder=lambda region, credentials: store,
        session_factory=session_factory,
    )

    assert recorded == 1
    assert built == [
        ("us-east-1", "https://sqs.us-east-1.amazonaws.com/123456789012/seskit-inbound")
    ]


async def test_the_job_with_nothing_set_up_builds_nothing(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await register_user(db_session, email="a@example.com", password=PASSWORD, allow_signup=True)
    await db_session.commit()

    def build(*args: Any) -> Any:
        raise AssertionError("nothing to poll, so nothing should be built")

    assert (
        await poll_inbound({}, build=build, store_builder=build, session_factory=session_factory)
        == 0
    )


async def test_one_unreachable_queue_does_not_abandon_the_others(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _receiving(db_session, "a@example.com", region="us-east-1")
    await _receiving(
        db_session,
        "b@example.com",
        region="eu-west-1",
        bucket="seskit-inbound-123456789012-eu-west-1",
        domain="example.net",
    )
    await db_session.commit()
    good = FakeNotificationQueue(_body(1))

    def build(region: str, queue_url: str, credentials: AWSCredentials) -> FakeNotificationQueue:
        if region == "eu-west-1":
            raise RuntimeError("could not reach it")
        return good

    recorded = await poll_inbound(
        {},
        build=build,
        store_builder=lambda region, credentials: FakeStore("seskit-x/1"),
        session_factory=session_factory,
    )

    assert recorded == 1


# ------------------------------------------------------------- webhooks ---


async def _with_endpoint(session: AsyncSession, connection: AWSConnection) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        project_id=connection.project_id,
        url="https://hooks.example.com/seskit",
        secret="whsec_test_secret",
        status="active",
    )
    session.add(endpoint)
    await session.commit()
    return endpoint


async def test_a_recorded_message_asks_the_queue_to_attempt_its_deliveries_now(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Latency only - the delivery row is what makes the webhook durable - but it
    is the difference between a webhook in seconds and one in a minute.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    await _with_endpoint(db_session, connection)
    enqueued: list[str] = []

    async def enqueue(delivery_id: str) -> None:
        enqueued.append(delivery_id)

    await _drain(
        FakeNotificationQueue(_body(1)),
        FakeStore("seskit-x/1"),
        session_factory,
        project_ids={connection.project_id},
        enqueue=enqueue,
    )

    deliveries = list(await db_session.scalars(select(WebhookDelivery)))
    assert len(deliveries) == 1
    assert enqueued == [deliveries[0].id]


async def test_a_duplicate_is_not_enqueued_a_second_time(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    await _with_endpoint(db_session, connection)
    enqueued: list[str] = []

    async def enqueue(delivery_id: str) -> None:
        enqueued.append(delivery_id)

    body = _body(1, sns_id="sns-same")
    await _drain(
        FakeNotificationQueue(body, body),
        FakeStore("seskit-x/1"),
        session_factory,
        project_ids={connection.project_id},
        enqueue=enqueue,
    )

    assert len(enqueued) == 1


async def test_a_project_with_no_endpoint_enqueues_nothing(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    enqueued: list[str] = []

    async def enqueue(delivery_id: str) -> None:
        enqueued.append(delivery_id)

    await _drain(
        FakeNotificationQueue(_body(1)),
        FakeStore("seskit-x/1"),
        session_factory,
        project_ids={connection.project_id},
        enqueue=enqueue,
    )

    assert enqueued == []


async def test_a_failed_enqueue_does_not_keep_a_recorded_message_on_the_queue(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The message is recorded and the delivery row exists, so the sweep will
    send the webhook within the minute. Leaving the announcement on the queue
    would only make it come back to be recognised as a duplicate.
    """
    connection, _ = await _receiving(db_session, "a@example.com")
    await db_session.commit()
    await _with_endpoint(db_session, connection)

    async def enqueue(delivery_id: str) -> None:
        raise ConnectionError("redis went away")

    queue = FakeNotificationQueue(_body(1))
    recorded = await _drain(
        queue,
        FakeStore("seskit-x/1"),
        session_factory,
        project_ids={connection.project_id},
        enqueue=enqueue,
    )

    assert recorded == 1
    assert queue.deleted == ["receipt-0"]
    assert await _stored(db_session) == 1
    assert len(list(await db_session.scalars(select(WebhookDelivery)))) == 1
