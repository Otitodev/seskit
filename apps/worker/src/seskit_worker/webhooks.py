"""Making the webhook request (§16).

The decisions - retry policy, backoff, auto-disable, what counts as terminal -
live in ``seskit_core.services.webhooks`` and are tested without a network. What
lives here is the part that can only be done with an HTTP client, and the three
properties of that request that are load-bearing.

**The connection goes to the address that was validated.** ``validate()``
resolves the hostname and checks every answer; this then builds a URL containing
that address, sends the original hostname in the ``Host`` header, and sets the
``sni_hostname`` extension so TLS still presents and verifies the certificate
for the *name*. Resolving, checking, and then handing the hostname to the client
to resolve again would leave a window where DNS answers differently in between,
which is the entire DNS-rebinding attack. Verified against a real server:
connecting to a bare IP with a mismatched ``sni_hostname`` fails with
``certificate is not valid for ...``, so the check is the name's, not the
address's.

**Redirects are never followed.** A redirect forwards the signed payload to a
host the user never registered - and the signature makes it look authentic when
it arrives there. `docs/prior-art.md` lists this; httpx follows redirects only
when asked, and it is asked not to, explicitly, so the default can never drift.

**The response body is bounded and read by streaming.** It is captured for the
delivery log and rendered into a dashboard page, so a hostile endpoint that
streams gigabytes would otherwise fill a column and a screen. Only text-ish
content types are stored at all.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from seskit_core.config import Settings, get_settings
from seskit_core.db import get_session_factory
from seskit_core.logging import get_logger
from seskit_core.models import DeliveryStatus, WebhookDelivery
from seskit_core.security.destinations import (
    Destination,
    DestinationError,
    IPAddress,
    parse_networks,
    validate,
)
from seskit_core.security.webhooks import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from seskit_core.services import (
    deliveries_due,
    payload_bytes,
    policy_from,
    record_delivery_failure,
    record_delivery_success,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = get_logger(__name__)

#: Builds the client used for one delivery. Injected so a test can substitute a
#: transport without patching a module attribute - the seam ``build_provider``
#: gives the send job.
ClientFactory = Callable[[float], httpx.AsyncClient]

USER_AGENT = "SESKit-Webhooks/1.0"

#: Response bodies worth keeping. Anything else is stored as nothing rather
#: than as mojibake in a dashboard table.
TEXTUAL_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/problem+json")

#: Answered explicitly rather than lumped in with 4xx: it means "later", which
#: is the definition of retryable.
TOO_MANY_REQUESTS = 429


def build_client(timeout: float) -> httpx.AsyncClient:
    """The client one delivery is made with.

    ``follow_redirects=False`` stated rather than relied upon: it is httpx's
    default today, and this is not a property to leave to a default.
    """
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


def pinned_url(url: str, address: IPAddress) -> str:
    """The original URL with its host replaced by the validated address.

    Scheme, port, path and query are preserved. IPv6 gets its brackets back -
    without them the netloc does not parse and the request goes nowhere.
    """
    parts = urlsplit(url)
    literal = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _is_textual(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(lowered.startswith(prefix) for prefix in TEXTUAL_CONTENT_TYPES)


async def _read_capped(response: httpx.Response, limit: int) -> str | None:
    """Read at most ``limit`` bytes of a response body.

    Streamed rather than buffered: ``response.content`` would pull the whole
    body into memory first, which is exactly what a hostile endpoint would like
    us to do.
    """
    if not _is_textual(response.headers.get("content-type", "")):
        return None

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break

    return b"".join(chunks)[:limit].decode("utf-8", errors="replace")


def _is_retryable(status: int) -> bool:
    """Whether another attempt could plausibly succeed.

    A 4xx means the endpoint understood the request and refused it; sending it
    five more times is noise in someone's logs. 429 is the exception - it
    explicitly means "later" - and 5xx is the server saying it failed, which is
    the case retries exist for.
    """
    if status == TOO_MANY_REQUESTS:
        return True
    return status >= 500


async def deliver_one(
    session: AsyncSession,
    delivery: WebhookDelivery,
    *,
    settings: Settings | None = None,
    build: ClientFactory | None = None,
    resolver: Any = None,
) -> DeliveryStatus:
    """Attempt one delivery and record what happened.

    Returns where the delivery ended up, so a caller can count outcomes. Never
    raises for a failed delivery - a webhook that cannot be delivered is an
    expected state, not an error - but does let an unexpected exception through,
    because that means we do not know what happened.
    """
    resolved = settings or get_settings()
    factory = build or build_client
    endpoint = delivery.endpoint

    if delivery.is_settled:
        # Already dealt with, most likely by the immediate attempt while the
        # sweep was picking the same row up.
        return delivery.state

    body = payload_bytes(delivery.event)

    try:
        destination = await validate(
            endpoint.url,
            policy=policy_from(
                is_local=resolved.is_local,
                allowed_networks=parse_networks(resolved.WEBHOOK_ALLOWED_CIDRS),
            ),
            resolver=resolver,
        )
    except DestinationError as error:
        # Terminal, deliberately. A URL that has started resolving to a private
        # address is not a transient fault, and retrying is precisely what must
        # not happen - that would be the SSRF attempt, repeated on a schedule.
        logger.warning("webhook_destination_refused", endpoint_id=endpoint.id)
        return await record_delivery_failure(
            session,
            delivery,
            endpoint,
            response_status=None,
            response_body=None,
            error=error.message,
            retryable=False,
            max_attempts=resolved.WEBHOOK_MAX_ATTEMPTS,
            base_seconds=resolved.WEBHOOK_RETRY_BASE_SECONDS,
            failure_limit=resolved.WEBHOOK_FAILURE_LIMIT,
        )

    signature, timestamp = sign(endpoint.secret, body)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: str(timestamp),
        # Convenience for a receiver routing on type without parsing the body,
        # and for correlating a redelivery. Neither is trusted: the body is what
        # the signature covers.
        "X-SESKit-Event-Id": delivery.event_id,
        "X-SESKit-Delivery-Id": delivery.id,
        # The real host, because the URL now carries an IP address.
        "Host": destination.host,
    }

    try:
        status, captured = await _post(
            factory,
            destination,
            body=body,
            headers=headers,
            timeout=float(resolved.WEBHOOK_TIMEOUT_SECONDS),
            capture_bytes=resolved.WEBHOOK_RESPONSE_CAPTURE_BYTES,
        )
    except httpx.HTTPError as error:
        # A timeout, a refused connection, a TLS failure. Worth another attempt:
        # none of them say the endpoint rejected the payload.
        logger.info(
            "webhook_transport_failed",
            delivery_id=delivery.id,
            error=type(error).__name__,
        )
        return await record_delivery_failure(
            session,
            delivery,
            endpoint,
            response_status=None,
            response_body=None,
            # The class name, not the exception text: a botocore-style message
            # can carry a URL or an address, and this is rendered into a page.
            error=type(error).__name__,
            retryable=True,
            max_attempts=resolved.WEBHOOK_MAX_ATTEMPTS,
            base_seconds=resolved.WEBHOOK_RETRY_BASE_SECONDS,
            failure_limit=resolved.WEBHOOK_FAILURE_LIMIT,
        )

    if 200 <= status < 300:
        await record_delivery_success(
            session, delivery, endpoint, response_status=status, response_body=captured
        )
        logger.info("webhook_delivered", delivery_id=delivery.id, status=status)
        return DeliveryStatus.DELIVERED

    return await record_delivery_failure(
        session,
        delivery,
        endpoint,
        response_status=status,
        response_body=captured,
        error=None,
        retryable=_is_retryable(status),
        max_attempts=resolved.WEBHOOK_MAX_ATTEMPTS,
        base_seconds=resolved.WEBHOOK_RETRY_BASE_SECONDS,
        failure_limit=resolved.WEBHOOK_FAILURE_LIMIT,
    )


async def _post(
    factory: ClientFactory,
    destination: Destination,
    *,
    body: bytes,
    headers: dict[str, str],
    # Handed to httpx, which applies it per connect/read/write phase.
    # ASYNC109 wants asyncio.timeout, but wrapping the whole call in one
    # would cancel mid-read and lose the distinction between a slow
    # endpoint and an unreachable one.
    timeout: float,  # noqa: ASYNC109
    capture_bytes: int,
) -> tuple[int, str | None]:
    """POST to the pinned address and read a bounded response."""
    url = pinned_url(destination.url, destination.pinned)

    async with factory(timeout) as client:
        request = client.build_request(
            "POST",
            url,
            content=body,
            headers=headers,
            # TLS presents and verifies the certificate for the hostname even
            # though the connection is to an address. Without this the
            # certificate would be checked against the IP and every delivery
            # would fail.
            extensions={"sni_hostname": destination.host},
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            captured = await _read_capped(response, capture_bytes)
        finally:
            await response.aclose()

    return response.status_code, captured


# ------------------------------------------------------------------- jobs ---


async def deliver_webhook(ctx: dict[str, Any], delivery_id: str) -> str:
    """ARQ entry point for one delivery, enqueued the moment it is created.

    Latency only. The row is what makes the delivery durable, so losing this job
    costs a few seconds rather than the webhook - the sweep picks it up.
    """
    factory = get_session_factory()
    async with factory() as session:
        delivery = await session.scalar(
            select(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .options(
                selectinload(WebhookDelivery.endpoint),
                selectinload(WebhookDelivery.event),
            )
        )
        if delivery is None:
            return DeliveryStatus.FAILED.value

        outcome = await deliver_one(session, delivery)
        await session.commit()
        return outcome.value


async def sweep_webhooks(ctx: dict[str, Any]) -> int:
    """Deliver everything that is due. Returns how many were attempted.

    This is what makes the delivery row rather than the ARQ job the source of
    truth: retries are due-dated on the row, and anything the immediate enqueue
    lost is picked up here on the next pass.
    """
    settings = get_settings()
    factory = get_session_factory()
    attempted = 0

    async with factory() as session:
        due = await deliveries_due(session)

        for delivery in due:
            try:
                await deliver_one(session, delivery, settings=settings)
                attempted += 1
            except Exception:
                # One endpoint behaving badly must not abandon the rest of the
                # pass - the same guard the identity recheck uses.
                logger.exception(
                    "webhook_delivery_failed",
                    delivery_id=delivery.id,
                    job_id=ctx.get("job_id"),
                )
            await session.commit()

    if attempted:
        logger.info("webhook_sweep", attempted=attempted)
    return attempted
