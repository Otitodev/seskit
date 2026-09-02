"""Ingesting delivery events from SQS (§15).

The default transport. SES publishes to a topic, the topic fans out to a queue,
and this drains the queue - which works behind NAT, on a laptop, with no
inbound port and no certificate, because §9 says SESKit has to run there.

**What acknowledges a message, and what does not.** A message is deleted only
once its outcome is *settled*: recorded, a known duplicate, or something no
amount of retrying will fix. Anything else is left alone and reappears when the
visibility timeout expires. That asymmetry is the whole design - `docs/prior-art.md`
records a comparable project that answered success on parse failures, so a
transient bug dropped events permanently and silently.

**A session per message.** ``ingest_event`` rolls back when two deliveries of the
same notification race, and a shared session would take the rest of the batch
down with it. One session per message is a few more checkouts from a pool that
exists for exactly this.

**A bounded pass.** Each run drains at most a configured number of batches. A
backlog must not monopolise the worker, because the job queued behind it is a
send, and a user notices a late email long before a late bounce receipt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from seskit_core.config import get_settings
from seskit_core.db import get_session_factory
from seskit_core.events import MalformedEnvelope, Outcome, ingest_event, unwrap
from seskit_core.logging import get_logger
from seskit_core.providers import NotificationQueue, QueuedNotification
from seskit_core.services import distinct_event_queues, pending_delivery_ids
from seskit_provider_aws_ses import SQSNotificationQueue
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

#: How the job turns a queue URL into a reader. Injectable so a test can
#: substitute one without patching a module attribute - the same seam
#: ``build_provider`` gives the send job.
QueueBuilder = Callable[[str, str], NotificationQueue]

#: How a session is opened. Injectable for the same reason the send job's was
#: split in Phase 6: a job that manages its own session cannot be tested
#: against a transaction the test can still see and roll back.
SessionFactory = Callable[[], AsyncSession]

#: Asks the queue to attempt a webhook delivery now. Optional: the row is
#: what makes the delivery durable, so a missing enqueue costs latency
#: rather than the webhook - the sweep picks it up within the minute.
Enqueue = Callable[[str], Awaitable[None]]


def build_queue(region: str, queue_url: str) -> NotificationQueue:
    """Map a region and queue URL onto a reader.

    Lives here rather than in core, which must not import a provider (§32.8).
    """
    return SQSNotificationQueue(region, queue_url)


async def poll_events(
    ctx: dict[str, Any],
    *,
    build: QueueBuilder | None = None,
    session_factory: SessionFactory | None = None,
    enqueue: Enqueue | None = None,
) -> int:
    """Drain every queue this instance has events on. Returns events recorded.

    Runs on a schedule rather than continuously. Long polling means each pass
    spends its time waiting rather than spinning, and a pass that finds nothing
    costs one request per queue.
    """
    settings = get_settings()
    if not settings.polls_sqs:
        return 0

    build = build or build_queue
    factory = session_factory or get_session_factory()
    enqueue = enqueue or _enqueue_via(ctx)
    recorded = 0

    async with factory() as session:
        queues = await distinct_event_queues(session)

    for region, queue_url in queues:
        try:
            recorded += await drain(
                build(region, queue_url),
                session_factory=factory,
                enqueue=enqueue,
                max_batches=settings.EVENT_POLL_MAX_BATCHES,
                wait_seconds=settings.EVENT_POLL_WAIT_SECONDS,
                visibility_timeout=settings.EVENT_VISIBILITY_TIMEOUT_SECONDS,
            )
        except Exception:
            # One unreachable queue must not abandon the others - the same
            # shape as the per-identity guard in the recheck pass.
            logger.exception("event_poll_failed", region=region, job_id=ctx.get("job_id"))

    if recorded:
        logger.info("event_poll_pass", recorded=recorded, queues=len(queues))
    return recorded


async def drain(
    queue: NotificationQueue,
    *,
    session_factory: SessionFactory,
    enqueue: Enqueue | None = None,
    max_batches: int,
    wait_seconds: int,
    visibility_timeout: int,
) -> int:
    """Read batches until the queue is empty or the budget runs out."""
    recorded = 0

    for _ in range(max(1, max_batches)):
        batch = await queue.receive(
            wait_seconds=wait_seconds, visibility_timeout=visibility_timeout
        )
        if not batch:
            # Long polling already waited; an empty batch means an empty queue,
            # and asking again immediately would only bill for the same answer.
            break

        for notification in batch:
            if await handle(queue, notification, session_factory=session_factory, enqueue=enqueue):
                recorded += 1

    return recorded


async def handle(
    queue: NotificationQueue,
    notification: QueuedNotification,
    *,
    session_factory: SessionFactory,
    enqueue: Enqueue | None = None,
) -> bool:
    """Process one message. Returns whether an event was recorded.

    Every path that reaches a decision deletes the message. The only path that
    leaves it on the queue is an unexpected failure, which is deliberately not
    caught here: an exception means we do not know what happened, and a message
    we cannot account for must come back.
    """
    try:
        envelope = unwrap(notification.body)
    except MalformedEnvelope:
        # Settled: redelivering something that is not JSON will not make it
        # JSON, and a queue that keeps trying stops making progress on
        # everything behind it.
        logger.warning("event_body_unreadable", queue_message_id=notification.queue_message_id)
        await queue.delete(notification)
        return False

    if not envelope.is_notification:
        # SNS confirms SQS subscriptions itself, so a handshake message here is
        # unexpected rather than routine - logged, and dropped, because nothing
        # in this path can answer it.
        logger.info("event_not_a_notification", message_type=envelope.message_type)
        await queue.delete(notification)
        return False

    if not envelope.event:
        logger.warning("event_payload_empty", sns_message_id=envelope.message_id)
        await queue.delete(notification)
        return False

    # A session per message: ingest_event rolls back when two deliveries of
    # the same notification race, and a shared session would take the rest of
    # the batch down with it.
    async with session_factory() as session:
        outcome, event = await ingest_event(
            session,
            envelope.event,
            # The envelope's id, not the queue's: SQS issues a new message id
            # per delivery, so keying on that would deduplicate nothing.
            provider_event_id=envelope.message_id or None,
        )
        # Read before the commit closes the session, so the ids survive.
        delivery_ids = (
            await pending_delivery_ids(session, event.id)
            if enqueue is not None and event is not None
            else []
        )
        await session.commit()

    for delivery_id in delivery_ids:
        # Latency only. A failure here loses seconds, not the webhook.
        await enqueue(delivery_id)  # type: ignore[misc]

    if outcome.is_settled:
        await queue.delete(notification)

    return outcome is Outcome.RECORDED


def _enqueue_via(ctx: dict[str, Any]) -> Enqueue | None:
    """Enqueue through the worker's own ARQ pool, if it has one.

    ARQ puts the pool on the job context. A test driving ``drain`` directly has
    no context and passes its own callback, or none at all.
    """
    pool = ctx.get("redis")
    if pool is None:
        return None

    async def enqueue(delivery_id: str) -> None:
        await pool.enqueue_job("deliver_webhook", delivery_id)

    return enqueue
