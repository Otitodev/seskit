"""Delivering webhooks (§16).

Two halves, tested where each belongs. The retry policy, the backoff and the
auto-disable rule are decisions and are asserted against the database with no
network at all. The request itself - what URL is connected to, which headers go
out, whether a redirect is followed - is asserted through an httpx
``MockTransport``, which records the request that would have gone on the wire.

The one that matters most is `test_the_connection_goes_to_the_validated_address`.
Everything in `security/destinations.py` is decoration if the client then
resolves the hostname again on its own: the check would pass against one address
and the connection would be made to another. That test asserts the URL actually
requested contains the address that was checked.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from seskit_core.config import Environment, Settings
from seskit_core.models import (
    DeliveryStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookStatus,
    utcnow,
)
from seskit_core.security.webhooks import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)
from seskit_core.services import (
    backoff_seconds,
    create_project,
    payload_bytes,
    queue_deliveries,
    register_user,
)
from seskit_worker.webhooks import deliver_one, pinned_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
PUBLIC = "93.184.216.34"
URL = "https://hooks.example.com/seskit"


def resolving_to(*addresses: str) -> object:
    def resolver(host: str) -> Sequence[str]:
        return list(addresses or (PUBLIC,))

    return resolver


class Recorder:
    """An httpx transport that records requests and answers on command."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"ok",
        content_type: str = "text/plain",
        error: Exception | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body
        self.content_type = content_type
        self.error = error

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(
            self.status, content=self.body, headers={"content-type": self.content_type}
        )

    def factory(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            timeout=timeout,
            follow_redirects=False,
        )


async def _setup(
    session: AsyncSession,
    *,
    url: str = URL,
    event_type: EventType = EventType.DELIVERED,
    status: WebhookStatus = WebhookStatus.ACTIVE,
) -> tuple[WebhookEndpoint, WebhookDelivery]:
    user = await register_user(
        session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(session, user_id=user.id, name="Hooks")
    email = Email(
        project_id=project.id,
        from_address="hello@example.com",
        to_addresses=["user@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hello",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id="ses-1",
    )
    session.add(email)
    await session.flush()

    event = EmailEvent(
        email_id=email.id,
        event_type=event_type.value,
        provider_event_id="sns-1",
        occurred_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        payload={
            "id": "evt_1",
            "type": f"email.{event_type.value}",
            "email_id": email.id,
            "data": {"to": ["user@example.com"]},
        },
    )
    endpoint = WebhookEndpoint(
        project_id=project.id, url=url, secret="whsec_test", status=status.value
    )
    session.add_all([event, endpoint])
    await session.flush()

    delivery = WebhookDelivery(
        webhook_endpoint_id=endpoint.id,
        event_id=event.id,
        status=DeliveryStatus.PENDING.value,
        next_attempt_at=utcnow(),
    )
    session.add(delivery)
    await session.flush()
    # The worker reads these off the row.
    delivery.endpoint = endpoint
    delivery.event = event
    return endpoint, delivery


def _settings(settings: Settings, **overrides: object) -> Settings:
    """Delivery settings for a test.

    ENVIRONMENT stays whatever the fixture gives (local), because that is what
    most of these tests want. The destination tests override it - the SSRF
    policy is deliberately relaxed in local development, so asserting a refusal
    only means something in a production configuration.
    """
    base: dict[str, object] = {"WEBHOOK_MAX_ATTEMPTS": 3, "WEBHOOK_RETRY_BASE_SECONDS": 5}
    base.update(overrides)
    return settings.model_copy(update=base)


# ------------------------------------------------------------- the request ---


async def test_a_delivery_is_signed_and_verifiable(
    db_session: AsyncSession, settings: Settings
) -> None:
    """End to end on the part a customer touches: re-compute the HMAC exactly
    as their own code would and confirm it matches what was sent.
    """
    endpoint, delivery = await _setup(db_session)
    recorder = Recorder()

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    request = recorder.requests[0]
    signature = request.headers[SIGNATURE_HEADER]
    timestamp = int(request.headers[TIMESTAMP_HEADER])

    assert verify(endpoint.secret, request.content, signature=signature, timestamp=timestamp)


async def test_the_body_is_the_stored_payload(db_session: AsyncSession, settings: Settings) -> None:
    """Byte-identical. Serialising once for signing and again for sending would
    eventually differ and every signature would fail for no visible reason.
    """
    _, delivery = await _setup(db_session)
    recorder = Recorder()

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    assert recorder.requests[0].content == payload_bytes(delivery.event)
    assert json.loads(recorder.requests[0].content)["type"] == "email.delivered"


async def test_the_connection_goes_to_the_validated_address(
    db_session: AsyncSession, settings: Settings
) -> None:
    """**The test that matters.**

    Validation resolves the hostname and checks every answer. If the client then
    resolved the name again on its own, the check would have passed against one
    address and the connection been made to another - which is the whole DNS
    rebinding attack. The request must name the address that was checked, and
    carry the hostname in Host so the far end still routes correctly.
    """
    _, delivery = await _setup(db_session)
    recorder = Recorder()

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    request = recorder.requests[0]
    assert request.url.host == PUBLIC
    assert request.headers["Host"] == "hooks.example.com"
    # TLS still verifies against the name, not the address.
    assert request.extensions["sni_hostname"] == "hooks.example.com"


async def test_the_path_and_port_survive_pinning() -> None:
    import ipaddress

    assert (
        pinned_url("https://hooks.example.com:8443/a/b?c=1", ipaddress.ip_address(PUBLIC))
        == f"https://{PUBLIC}:8443/a/b?c=1"
    )


async def test_useful_headers_are_sent(db_session: AsyncSession, settings: Settings) -> None:
    _, delivery = await _setup(db_session)
    recorder = Recorder()

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    headers = recorder.requests[0].headers
    assert headers["content-type"] == "application/json"
    assert headers["x-seskit-event-id"] == delivery.event_id
    assert "SESKit" in headers["user-agent"]


# ------------------------------------------------------------- destinations ---


async def test_a_destination_that_now_resolves_internally_is_refused(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The rebinding case, at delivery time. Nothing is sent, and it is terminal
    rather than retried - retrying would be the SSRF attempt on a schedule.

    Run in a production configuration on purpose: the policy is relaxed in local
    development, so this assertion would pass vacuously against the default
    fixture.
    """
    _, delivery = await _setup(db_session)
    recorder = Recorder()

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings, ENVIRONMENT=Environment.PRODUCTION),
        build=recorder.factory,
        resolver=resolving_to("127.0.0.1"),
    )

    assert outcome is DeliveryStatus.FAILED
    # Not merely refused after the fact - never requested at all.
    assert recorder.requests == []
    assert delivery.next_attempt_at is None
    assert delivery.error


async def test_local_development_may_deliver_to_a_private_address(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The other half of the graded policy, stated so the trade-off is visible.

    A receiver on localhost is how anyone tries webhooks at all, and self-hosted
    software that cannot be tested locally will not be adopted. The protection
    is switched on by ENVIRONMENT, not by hope.
    """
    _, delivery = await _setup(db_session, url="http://localhost:9000/hook")
    recorder = Recorder()

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings, ENVIRONMENT=Environment.LOCAL),
        build=recorder.factory,
        resolver=resolving_to("127.0.0.1"),
    )

    assert outcome is DeliveryStatus.DELIVERED
    assert recorder.requests[0].url.host == "127.0.0.1"


async def test_an_allowlisted_range_is_delivered_to_in_production(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The deliberate escape hatch, end to end."""
    _, delivery = await _setup(db_session, url="https://internal.example.com/hook")
    recorder = Recorder()

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(
            settings,
            ENVIRONMENT=Environment.PRODUCTION,
            WEBHOOK_ALLOWED_CIDRS="10.10.0.0/16",
        ),
        build=recorder.factory,
        resolver=resolving_to("10.10.1.1"),
    )

    assert outcome is DeliveryStatus.DELIVERED


async def test_a_redirect_is_not_followed(db_session: AsyncSession, settings: Settings) -> None:
    """A redirect forwards the signed payload to a host the user never
    registered - and the signature makes it look authentic when it arrives
    there. `docs/design/prior-art.md` lists this; here the redirect points at the cloud
    metadata service, which is what makes it worth more than a style note.

    The 302 is recorded as a failure rather than chased.
    """
    _, delivery = await _setup(db_session)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout, follow_redirects=False
        )

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings, ENVIRONMENT=Environment.PRODUCTION),
        build=factory,
        resolver=resolving_to(PUBLIC),
    )

    # Exactly one request, to the registered host. The redirect target was never
    # contacted.
    assert len(seen) == 1
    assert seen[0].url.host == PUBLIC
    assert outcome is DeliveryStatus.FAILED
    assert delivery.response_status == 302


# ------------------------------------------------------------------ outcomes ---


@pytest.mark.parametrize("status", [200, 201, 202, 204])
async def test_a_2xx_is_delivered(
    db_session: AsyncSession, settings: Settings, status: int
) -> None:
    endpoint, delivery = await _setup(db_session)

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=Recorder(status=status).factory,
        resolver=resolving_to(PUBLIC),
    )

    assert outcome is DeliveryStatus.DELIVERED
    assert delivery.response_status == status
    assert delivery.next_attempt_at is None
    assert endpoint.consecutive_failures == 0


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_a_retryable_status_schedules_another_attempt(
    db_session: AsyncSession, settings: Settings, status: int
) -> None:
    """429 is in this list on purpose: it means "later", which is the definition
    of retryable, even though it is a 4xx.
    """
    _, delivery = await _setup(db_session)

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=Recorder(status=status).factory,
        resolver=resolving_to(PUBLIC),
    )

    assert outcome is DeliveryStatus.PENDING
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at is not None
    assert delivery.next_attempt_at > utcnow()


@pytest.mark.parametrize("status", [400, 403, 404, 410, 422])
async def test_a_client_error_is_terminal(
    db_session: AsyncSession, settings: Settings, status: int
) -> None:
    """The endpoint understood and refused. Sending it five more times is noise
    in someone else's logs.
    """
    _, delivery = await _setup(db_session)

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=Recorder(status=status).factory,
        resolver=resolving_to(PUBLIC),
    )

    assert outcome is DeliveryStatus.FAILED
    assert delivery.next_attempt_at is None


async def test_a_transport_failure_is_retried(db_session: AsyncSession, settings: Settings) -> None:
    """A timeout says nothing about whether the payload was acceptable."""
    _, delivery = await _setup(db_session)
    recorder = Recorder(error=httpx.ConnectTimeout("too slow"))

    outcome = await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    assert outcome is DeliveryStatus.PENDING
    # The class name, not the exception text: that can carry a URL or an
    # address, and this is rendered into a page.
    assert delivery.error == "ConnectTimeout"


async def test_attempts_run_out(db_session: AsyncSession, settings: Settings) -> None:
    _, delivery = await _setup(db_session)
    configured = _settings(settings, WEBHOOK_MAX_ATTEMPTS=3)
    recorder = Recorder(status=500)

    outcomes = []
    for _ in range(3):
        delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
        outcomes.append(
            await deliver_one(
                db_session,
                delivery,
                settings=configured,
                build=recorder.factory,
                resolver=resolving_to(PUBLIC),
            )
        )

    assert outcomes == [DeliveryStatus.PENDING, DeliveryStatus.PENDING, DeliveryStatus.FAILED]
    assert delivery.attempt_count == 3


# ---------------------------------------------------------- response capture ---


async def test_a_huge_response_is_truncated(db_session: AsyncSession, settings: Settings) -> None:
    """A hostile endpoint would otherwise fill a column that is rendered into a
    dashboard page.
    """
    _, delivery = await _setup(db_session)
    recorder = Recorder(body=b"x" * 5_000_000)

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings, WEBHOOK_RESPONSE_CAPTURE_BYTES=1024),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    assert delivery.response_body is not None
    assert len(delivery.response_body) <= 1024


async def test_a_binary_response_is_not_stored(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, delivery = await _setup(db_session)
    recorder = Recorder(body=b"\x89PNG\r\n", content_type="image/png")

    await deliver_one(
        db_session,
        delivery,
        settings=_settings(settings),
        build=recorder.factory,
        resolver=resolving_to(PUBLIC),
    )

    assert delivery.response_body is None


# -------------------------------------------------------------- auto-disable ---


async def test_an_endpoint_switches_off_after_repeated_failures(
    db_session: AsyncSession, settings: Settings
) -> None:
    endpoint, delivery = await _setup(db_session)
    configured = _settings(settings, WEBHOOK_MAX_ATTEMPTS=1, WEBHOOK_FAILURE_LIMIT=3)
    recorder = Recorder(status=500)

    for _ in range(3):
        delivery.status = DeliveryStatus.PENDING.value
        delivery.attempt_count = 0
        await deliver_one(
            db_session,
            delivery,
            settings=configured,
            build=recorder.factory,
            resolver=resolving_to(PUBLIC),
        )

    assert endpoint.was_disabled_by_failures is True
    assert endpoint.is_enabled is False


async def test_a_success_resets_the_failure_count(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Consecutive, not total - otherwise a long-lived endpoint is eventually
    disabled for a bad week it recovered from months ago.
    """
    endpoint, delivery = await _setup(db_session)
    configured = _settings(settings, WEBHOOK_MAX_ATTEMPTS=1, WEBHOOK_FAILURE_LIMIT=3)

    await deliver_one(
        db_session,
        delivery,
        settings=configured,
        build=Recorder(status=500).factory,
        resolver=resolving_to(PUBLIC),
    )
    assert endpoint.consecutive_failures == 1

    delivery.status = DeliveryStatus.PENDING.value
    await deliver_one(
        db_session,
        delivery,
        settings=configured,
        build=Recorder(status=200).factory,
        resolver=resolving_to(PUBLIC),
    )

    assert endpoint.consecutive_failures == 0
    assert endpoint.is_enabled is True


# ----------------------------------------------------------------- backoff ---


def test_backoff_grows_and_stays_inside_its_jitter() -> None:
    """Doubling, spread by ±30%. The jitter is what stops every endpoint on a
    shared host retrying in lockstep and knocking it over as it recovers.
    """
    for attempt, centre in [(1, 5), (2, 10), (3, 20), (4, 40)]:
        samples = [backoff_seconds(attempt, base=5) for _ in range(50)]
        assert all(centre * 0.7 - 0.01 <= s <= centre * 1.3 + 0.01 for s in samples)
        # And it is actually random, not a constant that happens to be in range.
        assert len(set(samples)) > 1


def test_backoff_is_never_shorter_than_a_second() -> None:
    assert backoff_seconds(1, base=1) >= 1.0


# ----------------------------------------------------------------- queueing ---


async def test_queueing_creates_one_delivery_per_enabled_endpoint(
    db_session: AsyncSession,
) -> None:
    endpoint, delivery = await _setup(db_session)
    event = delivery.event
    # A second endpoint on the same project, and one that is switched off.
    db_session.add_all(
        [
            WebhookEndpoint(
                project_id=endpoint.project_id, url="https://two.example.com/x", secret="whsec_2"
            ),
            WebhookEndpoint(
                project_id=endpoint.project_id,
                url="https://off.example.com/x",
                secret="whsec_3",
                status=WebhookStatus.DISABLED_BY_USER.value,
            ),
        ]
    )
    await db_session.flush()

    created = await queue_deliveries(db_session, event)

    # One for the second endpoint; the first already has one, and the disabled
    # endpoint gets nothing.
    assert len(created) == 1


async def test_queueing_twice_yields_one_delivery(db_session: AsyncSession) -> None:
    """The unique constraint doing its job. Both ingestion transports can queue,
    and a redelivered SES notification must not become a second webhook.
    """
    _, delivery = await _setup(db_session)
    event = delivery.event

    await queue_deliveries(db_session, event)
    await queue_deliveries(db_session, event)

    total = await db_session.scalar(
        select(func.count())
        .select_from(WebhookDelivery)
        .where(WebhookDelivery.event_id == event.id)
    )
    assert total == 1


async def test_a_non_public_event_type_is_not_delivered(db_session: AsyncSession) -> None:
    """§16 promises six types. `rejected` is recorded but not shipped - sending
    it would make it a contract by accident.
    """
    _, delivery = await _setup(db_session, event_type=EventType.REJECTED)
    await db_session.delete(delivery)
    await db_session.flush()

    assert await queue_deliveries(db_session, delivery.event) == []


async def test_another_projects_endpoint_gets_nothing(db_session: AsyncSession) -> None:
    """The tenancy boundary. An event belongs to one project's mail."""
    _, delivery = await _setup(db_session)
    stranger = await register_user(
        db_session, email="them@example.com", password=PASSWORD, allow_signup=True
    )
    other = await create_project(db_session, user_id=stranger.id, name="Theirs")
    db_session.add(
        WebhookEndpoint(project_id=other.id, url="https://theirs.example.com/x", secret="whsec_x")
    )
    await db_session.flush()

    created = await queue_deliveries(db_session, delivery.event)

    assert created == []
