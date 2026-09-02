"""Managing webhook endpoints, and scheduling their deliveries (§16).

The decisions live here; the HTTP request lives in the worker. That split is
what lets retry policy, auto-disable and the queueing rule be tested without a
network, and it is the same shape ``services/events.py`` uses for provisioning.

**Backoff carries jitter, and the jitter is not decoration.** Endpoints
frequently share a host - three projects pointing at the same SaaS, or a dozen
instances at one customer. Without jitter every delivery that failed at the same
moment retries at the same moment, so a service coming back up is immediately
knocked over again by the herd it just dropped. `docs/prior-art.md` records 30%,
which is enough to smear a wave across a window without making the schedule
unpredictable to a human reading the log.

**Consecutive failures are counted per delivery, not per attempt.** A delivery
that fails twice and succeeds on the third try is a success; counting attempts
would disable a healthy endpoint for surviving a blip.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seskit_core.logging import get_logger
from seskit_core.models import (
    DeliveryStatus,
    Email,
    EmailEvent,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookStatus,
    utcnow,
)
from seskit_core.models.email_event import PUBLIC_EVENT_TYPES
from seskit_core.security.destinations import DestinationPolicy, validate
from seskit_core.security.webhooks import generate_secret

logger = get_logger(__name__)

#: Proportion of a backoff interval to spread randomly. See the module
#: docstring - this is what stops a thundering herd on a shared host.
JITTER = 0.3


def payload_bytes(event: EmailEvent) -> bytes:
    """The exact bytes to sign and to send.

    One function, because the signature covers what goes on the wire and the two
    must be produced identically. Serialising once for signing and again for
    sending would eventually differ - a re-ordered key or a changed separator -
    and every signature would fail verification for no visible reason.

    Compact separators, and key order left as stored: the payload is a dict
    built by ``to_public`` in a fixed order, and re-sorting it here would change
    the bytes for no benefit.
    """
    return json.dumps(event.payload, separators=(",", ":")).encode("utf-8")


def backoff_seconds(attempt: int, *, base: int, jitter: float = JITTER) -> float:
    """How long to wait before attempt ``attempt`` + 1.

    ``base * 2^(attempt-1)``, spread by ±``jitter``. With a five second base
    that is roughly 5, 10, 20, 40, 80, 160 - about five minutes over six
    attempts, which covers a deploy but not an outage.
    """
    interval = base * (2 ** max(0, attempt - 1))
    spread = interval * jitter
    return max(1.0, random.uniform(interval - spread, interval + spread))  # noqa: S311


# ------------------------------------------------------------- endpoints ---


def policy_from(*, is_local: bool, allowed_networks: tuple[object, ...] = ()) -> DestinationPolicy:
    """Build the destination policy for this instance.

    Local development may reach private addresses and may use plain HTTP,
    because a receiver on localhost is how anyone tries the feature at all.
    Everything else is strict unless an operator has said otherwise.
    """
    return DestinationPolicy(
        allow_private=is_local,
        allowed_networks=allowed_networks,  # type: ignore[arg-type]
        require_https=not is_local,
    )


async def create_endpoint(
    session: AsyncSession,
    *,
    project_id: str,
    url: str,
    policy: DestinationPolicy,
) -> WebhookEndpoint:
    """Register a destination, refusing anything SESKit must not send to.

    Validation here is a courtesy: it puts the error on the form rather than in
    a log an hour later. The check that actually protects the network runs again
    at every delivery, against the resolved address - see
    ``security/destinations.py``.
    """
    url = url.strip()
    validate(url, policy=policy)

    endpoint = WebhookEndpoint(project_id=project_id, url=url, secret=generate_secret())
    session.add(endpoint)
    await session.flush()

    logger.info("webhook_endpoint_created", endpoint_id=endpoint.id, project_id=project_id)
    return endpoint


async def list_endpoints(session: AsyncSession, project_id: str) -> list[WebhookEndpoint]:
    rows = await session.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.project_id == project_id)
        .order_by(WebhookEndpoint.created_at.desc())
    )
    return list(rows)


async def get_owned_endpoint(
    session: AsyncSession, *, project_id: str, endpoint_id: str
) -> WebhookEndpoint | None:
    """One endpoint, if it belongs to this project.

    Ownership is part of the query rather than a check afterwards, so an id from
    another project resolves to nothing instead of to someone else's row.
    """
    endpoint: WebhookEndpoint | None = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.project_id == project_id,
        )
    )
    return endpoint


async def set_enabled(
    session: AsyncSession, endpoint: WebhookEndpoint, *, enabled: bool
) -> WebhookEndpoint:
    """Turn an endpoint on or off by hand.

    Re-enabling clears the failure count as well as the status: a user who has
    fixed their endpoint should get a full allowance, not one attempt before it
    switches off again.
    """
    if enabled:
        endpoint.status = WebhookStatus.ACTIVE.value
        endpoint.consecutive_failures = 0
    else:
        endpoint.status = WebhookStatus.DISABLED_BY_USER.value

    await session.flush()
    logger.info("webhook_endpoint_status", endpoint_id=endpoint.id, status=endpoint.status)
    return endpoint


async def rotate_secret(session: AsyncSession, endpoint: WebhookEndpoint) -> WebhookEndpoint:
    """Issue a new signing secret.

    Immediate and total: deliveries in flight signed with the old secret will
    fail verification at the receiver. That is the correct behaviour for a
    secret being rotated because it leaked, and the UI says so before doing it.
    """
    endpoint.secret = generate_secret()
    await session.flush()
    logger.info("webhook_secret_rotated", endpoint_id=endpoint.id)
    return endpoint


async def delete_endpoint(session: AsyncSession, endpoint: WebhookEndpoint) -> None:
    await session.delete(endpoint)
    await session.flush()
    logger.info("webhook_endpoint_deleted", endpoint_id=endpoint.id)


# -------------------------------------------------------------- queueing ---


async def queue_deliveries(session: AsyncSession, event: EmailEvent) -> list[WebhookDelivery]:
    """Create a delivery row per enabled endpoint on the event's project.

    The seam Phase 7 left. Called after an event is recorded, by whichever
    transport recorded it.

    Only §16's six public types are delivered. `rejected`, `delivery_delayed`
    and `rendering_failed` stay recorded but undelivered: the public API does
    not promise them, and shipping them would make them a contract by accident.
    """
    if event.type not in PUBLIC_EVENT_TYPES:
        return []

    project_id = await session.scalar(select(Email.project_id).where(Email.id == event.email_id))
    if project_id is None:
        return []

    endpoints = await session.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.project_id == project_id,
            WebhookEndpoint.status == WebhookStatus.ACTIVE.value,
        )
    )

    created: list[WebhookDelivery] = []
    for endpoint in endpoints:
        delivery = WebhookDelivery(
            webhook_endpoint_id=endpoint.id,
            event_id=event.id,
            status=DeliveryStatus.PENDING.value,
            next_attempt_at=utcnow(),
        )
        try:
            # A savepoint, emphatically not session.rollback(). This runs inside
            # ingest_event's transaction, so rolling the session back on a
            # duplicate would discard the EmailEvent that was just recorded and
            # every delivery queued before it - losing the event to save a row
            # that was already there.
            async with session.begin_nested():
                session.add(delivery)
                await session.flush()
        except IntegrityError:
            # Already queued for this endpoint. The unique constraint is what
            # makes a redelivered SES notification harmless rather than a
            # second webhook, so this is routine, not an error.
            continue
        created.append(delivery)

    if created:
        logger.info(
            "webhook_deliveries_queued",
            event_id=event.id,
            event_type=event.event_type,
            count=len(created),
        )
    return created


async def pending_delivery_ids(session: AsyncSession, event_id: str) -> list[str]:
    """Deliveries queued for one event and not yet attempted.

    Exists so a caller can ask ARQ to attempt them now rather than waiting for
    the sweep. Core must not import a queue (§32.8), so the enqueueing itself
    belongs to the app - this only says what there is to enqueue.
    """
    rows = await session.scalars(
        select(WebhookDelivery.id).where(
            WebhookDelivery.event_id == event_id,
            WebhookDelivery.status == DeliveryStatus.PENDING.value,
        )
    )
    return list(rows)


async def deliveries_due(session: AsyncSession, *, limit: int = 100) -> list[WebhookDelivery]:
    """Pending deliveries whose time has come, oldest first.

    Oldest first so a backlog drains in the order it built up - a customer
    replaying their log should see events in roughly the order they happened.
    """
    rows = await session.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == DeliveryStatus.PENDING.value,
            WebhookDelivery.next_attempt_at <= utcnow(),
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .options(
            selectinload(WebhookDelivery.endpoint),
            selectinload(WebhookDelivery.event),
        )
    )
    return list(rows)


async def list_deliveries(
    session: AsyncSession, *, endpoint_id: str, limit: int = 50
) -> list[WebhookDelivery]:
    """Delivery history for one endpoint, newest first (§17)."""
    rows = await session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
        .options(selectinload(WebhookDelivery.event))
    )
    return list(rows)


# --------------------------------------------------------------- outcomes ---


async def record_delivery_success(
    session: AsyncSession,
    delivery: WebhookDelivery,
    endpoint: WebhookEndpoint,
    *,
    response_status: int,
    response_body: str | None,
) -> None:
    """The endpoint accepted it."""
    delivery.status = DeliveryStatus.DELIVERED.value
    delivery.attempt_count += 1
    delivery.response_status = response_status
    delivery.response_body = response_body
    delivery.error = None
    delivery.last_attempt_at = utcnow()
    # Null so the row leaves the sweep's index. A settled delivery is not work.
    delivery.next_attempt_at = None

    # One success clears the slate. Counting consecutive failures rather than
    # total is what stops a long-lived endpoint being disabled for a bad week it
    # recovered from months ago.
    endpoint.consecutive_failures = 0

    await session.flush()


async def record_delivery_failure(
    session: AsyncSession,
    delivery: WebhookDelivery,
    endpoint: WebhookEndpoint,
    *,
    response_status: int | None,
    response_body: str | None,
    error: str | None,
    retryable: bool,
    max_attempts: int,
    base_seconds: int,
    failure_limit: int,
) -> DeliveryStatus:
    """One attempt did not succeed. Returns where the delivery ended up.

    A retryable failure with attempts left schedules the next one and leaves the
    delivery pending. Anything else is terminal, and terminal failures are what
    count toward disabling the endpoint.
    """
    delivery.attempt_count += 1
    delivery.response_status = response_status
    delivery.response_body = response_body
    delivery.error = error
    delivery.last_attempt_at = utcnow()

    if retryable and delivery.attempt_count < max_attempts:
        wait = backoff_seconds(delivery.attempt_count, base=base_seconds)
        delivery.next_attempt_at = utcnow() + timedelta(seconds=wait)
        await session.flush()
        return DeliveryStatus.PENDING

    delivery.status = DeliveryStatus.FAILED.value
    delivery.next_attempt_at = None

    endpoint.consecutive_failures += 1
    if endpoint.consecutive_failures >= failure_limit and endpoint.is_enabled:
        # A status of its own, not the one a user sets. The dashboard has to be
        # able to say *why* it stopped and offer to turn it back on - a switch
        # that appears to have moved by itself is the version that generates
        # support questions.
        endpoint.status = WebhookStatus.DISABLED_AFTER_FAILURES.value
        logger.warning(
            "webhook_endpoint_auto_disabled",
            endpoint_id=endpoint.id,
            failures=endpoint.consecutive_failures,
        )

    await session.flush()
    return DeliveryStatus.FAILED


__all__ = [
    "JITTER",
    "backoff_seconds",
    "create_endpoint",
    "delete_endpoint",
    "deliveries_due",
    "get_owned_endpoint",
    "list_deliveries",
    "list_endpoints",
    "payload_bytes",
    "pending_delivery_ids",
    "policy_from",
    "queue_deliveries",
    "record_delivery_failure",
    "record_delivery_success",
    "rotate_secret",
    "set_enabled",
]
