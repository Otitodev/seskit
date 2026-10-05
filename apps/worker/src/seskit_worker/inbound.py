"""Draining received mail off the queue.

The counterpart of ``events.py`` for mail that arrives. SES stores each message
in the project's bucket and announces it on a topic; the topic writes to a
queue; this reads the queue, fetches the message the announcement points at,
and records it.

**What acknowledges a message, and what does not** - the same asymmetry
``events.py`` documents, and for the same reason. A message is deleted only once
its outcome is *settled*: recorded, already recorded, not ours, not mail, or
expired. Anything else is left on the queue and reappears when the visibility
timeout runs out. Deleting too early loses mail permanently; never deleting
stops the queue making progress.

**The order is chosen so that the expensive step comes last.** Whose message it
is, and whether it is already stored, are database lookups. The download is a
network transfer of up to 40 MB. A redelivery, a message for somebody else's
domain, or a notification that is not mail at all never reaches it.

**A failure on one message does not stop the others.** ``events.py`` lets an
unexpected exception end the pass. Here the step that can fail is a download
from S3, and the commonest cause is a permission - a policy missing
``s3:GetObject`` - which would fail the same way on every message. If that
aborted the batch, one unreadable message at the front would stall everything
behind it. So each message is isolated: a failure is logged by type, the
message is left for the next pass, and the rest of the batch is handled.

**A session per message**, for the same reason as in ``events.py``, and two
short ones rather than one long one: the lookups, then the download with no
database connection held, then the write.

The original is never deleted from the bucket here. Retention does that.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from seskit_core.config import get_settings
from seskit_core.db import get_session_factory
from seskit_core.errors import APIError, ErrorType
from seskit_core.events import (
    MalformedEnvelope,
    NotAReceipt,
    Outcome,
    is_recorded,
    locate,
    parse_received,
    record_received,
    unwrap,
)
from seskit_core.logging import get_logger
from seskit_core.providers import (
    AWSCredentials,
    InboundStore,
    NotificationQueue,
    QueuedNotification,
)
from seskit_core.services.inbound import distinct_inbound_queues
from seskit_provider_aws_ses import S3InboundStore

from seskit_worker.events import SessionFactory, build_queue

logger = get_logger(__name__)

#: How the job turns a region and key into a reader of stored messages.
#: Injectable, like ``QueueBuilder``, so a test can substitute one.
StoreBuilder = Callable[[str, AWSCredentials], InboundStore]


def build_store(region: str, credentials: AWSCredentials) -> InboundStore:
    """Map a region and access key onto a reader of stored mail.

    Lives here rather than in core, which must not import a provider.
    """
    return S3InboundStore(region, credentials)


async def poll_inbound(
    ctx: dict[str, Any],
    *,
    build: Callable[[str, str, AWSCredentials], NotificationQueue] | None = None,
    store_builder: StoreBuilder | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """Drain every inbound queue this instance has. Returns messages recorded.

    Gated on the same setting as the event poller: receiving is announced over
    SQS only, so an instance configured for HTTPS-only ingestion has nothing to
    poll and says nothing about it.
    """
    settings = get_settings()
    if not settings.polls_sqs:
        return 0

    build = build or build_queue
    store_builder = store_builder or build_store
    factory = session_factory or get_session_factory()
    recorded = 0

    async with factory() as session:
        inboxes = await distinct_inbound_queues(session, secret_key=settings.SECRET_KEY)

    for inbox in inboxes:
        try:
            recorded += await drain_inbound(
                build(inbox.region, inbox.queue_url, inbox.credentials),
                store_builder(inbox.region, inbox.credentials),
                bucket=inbox.bucket,
                session_factory=factory,
                project_ids=inbox.project_ids,
                retention_days=settings.INBOUND_RETENTION_DAYS,
                max_batches=settings.EVENT_POLL_MAX_BATCHES,
                wait_seconds=settings.EVENT_POLL_WAIT_SECONDS,
                visibility_timeout=settings.EVENT_VISIBILITY_TIMEOUT_SECONDS,
            )
        except Exception:
            # One unreachable queue must not abandon the others.
            logger.exception("inbound_poll_failed", region=inbox.region, job_id=ctx.get("job_id"))

    if recorded:
        logger.info("inbound_poll_pass", recorded=recorded, queues=len(inboxes))
    return recorded


async def drain_inbound(
    queue: NotificationQueue,
    store: InboundStore,
    *,
    bucket: str,
    session_factory: SessionFactory,
    project_ids: Collection[str],
    retention_days: int,
    max_batches: int,
    wait_seconds: int,
    visibility_timeout: int,
) -> int:
    """Read batches until the queue is empty or the budget runs out.

    A bounded pass, as in ``events.py``: a backlog must not monopolise the
    worker, because the job queued behind it is a send.
    """
    recorded = 0

    for _ in range(max(1, max_batches)):
        batch = await queue.receive(
            wait_seconds=wait_seconds, visibility_timeout=visibility_timeout
        )
        if not batch:
            # Long polling already waited; an empty batch means an empty queue.
            break

        for notification in batch:
            try:
                handled = await handle_inbound(
                    queue,
                    store,
                    notification,
                    bucket=bucket,
                    session_factory=session_factory,
                    project_ids=project_ids,
                    retention_days=retention_days,
                )
            except Exception as exc:
                # Left on the queue, and the rest of the batch carries on. The
                # type and the queue's own id only: an exception raised while
                # handling mail can carry a fragment of it.
                logger.warning(
                    "inbound_message_failed",
                    error=type(exc).__name__,
                    queue_message_id=notification.queue_message_id,
                )
                continue
            if handled:
                recorded += 1

    return recorded


async def handle_inbound(
    queue: NotificationQueue,
    store: InboundStore,
    notification: QueuedNotification,
    *,
    bucket: str,
    session_factory: SessionFactory,
    project_ids: Collection[str],
    retention_days: int,
) -> bool:
    """Process one announcement. Returns whether a message was recorded.

    Every path that reaches a decision deletes the announcement. The paths that
    leave it on the queue are the ones that raise: a download that failed for a
    reason that may pass, or a database that did not answer.
    """
    try:
        envelope = unwrap(notification.body)
    except MalformedEnvelope:
        # Settled: redelivering something that is not JSON will not make it JSON.
        logger.warning("inbound_body_unreadable", queue_message_id=notification.queue_message_id)
        await queue.delete(notification)
        return False

    if not envelope.is_notification or not envelope.event:
        logger.info("inbound_not_a_notification", message_type=envelope.message_type)
        await queue.delete(notification)
        return False

    try:
        received = parse_received(envelope.event)
    except NotAReceipt:
        # Mail the topic carried that is not a stored message to record - a
        # different action's notification, say. Nothing to fetch.
        logger.info("inbound_not_a_receipt", sns_message_id=envelope.message_id)
        await queue.delete(notification)
        return False

    event_id = envelope.message_id or None

    # The cheap questions first, with no download and no connection held after.
    async with session_factory() as session:
        identity = await locate(session, received, bucket=bucket, project_ids=project_ids)
        already = identity is not None and await is_recorded(
            session, provider_event_id=event_id, bucket=received.bucket, key=received.key
        )

    if identity is None:
        # Not a domain this queue speaks for. Dropped, and the same whatever the
        # reason, so a probe learns nothing from the difference.
        logger.info("inbound_not_ours", sns_message_id=envelope.message_id)
        await queue.delete(notification)
        return False

    if already:
        await queue.delete(notification)
        return False

    try:
        raw = await store.fetch_message(bucket=received.bucket, key=received.key)
    except APIError as error:
        if error.error_type is ErrorType.NOT_FOUND:
            # Expired, or never there. Settled: it will not come back, and a
            # queue that keeps redelivering it makes no progress on the rest.
            logger.info("inbound_object_gone", sns_message_id=envelope.message_id)
            await queue.delete(notification)
            return False
        # Anything else may pass - a permission being added, a network back.
        raise

    async with session_factory() as session:
        outcome, _ = await record_received(
            session,
            identity,
            received,
            raw,
            provider_event_id=event_id,
            retention_days=retention_days,
        )
        await session.commit()

    if outcome.is_settled:
        await queue.delete(notification)

    return outcome is Outcome.RECORDED
