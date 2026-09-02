"""``GET /v1/webhooks`` (§23).

Read-only by design: registering a destination means asking SESKit to make
requests to a URL, which stays behind a session and a CSRF token for the same
reason API key issuance does.

The assertion that carries the most weight is
`test_the_signing_secret_is_never_returned`. The dashboard shows the secret in
full - a person reads it once while configuring a receiver - but an API response
travels into logs, traces and error reports, and a signing secret that travels
that way is one an attacker eventually reads without ever seeing the dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from seskit_core.models import (
    DeliveryStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookStatus,
)
from seskit_core.services import create_api_key, create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
WEBHOOKS_URL = "/v1/webhooks"
SECRET = "whsec_never_in_a_response"


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _project_with_key(
    session: AsyncSession, *, email: str = "owner@example.com"
) -> tuple[str, str]:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Hooks")
    issued = await create_api_key(session, project_id=project.id, name="production")
    return project.id, issued.raw_key


async def _endpoint(
    session: AsyncSession,
    project_id: str,
    *,
    url: str = "https://hooks.example.com/seskit",
    status: WebhookStatus = WebhookStatus.ACTIVE,
    failures: int = 0,
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        project_id=project_id,
        url=url,
        secret=SECRET,
        status=status.value,
        consecutive_failures=failures,
    )
    session.add(endpoint)
    await session.flush()
    return endpoint


async def _delivery(
    session: AsyncSession, project_id: str, endpoint: WebhookEndpoint, **overrides: object
) -> WebhookDelivery:
    email = Email(
        project_id=project_id,
        from_address="hello@example.com",
        to_addresses=["user@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hi",
        status=EmailStatus.SENT.value,
        provider_message_id="ses-1",
    )
    session.add(email)
    await session.flush()
    event = EmailEvent(
        email_id=email.id,
        event_type=EventType.DELIVERED.value,
        provider_event_id=f"sns-{endpoint.id}",
        occurred_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        payload={"type": "email.delivered", "data": {}},
    )
    session.add(event)
    await session.flush()

    fields: dict[str, object] = {
        "webhook_endpoint_id": endpoint.id,
        "event_id": event.id,
        "status": DeliveryStatus.DELIVERED.value,
        "attempt_count": 1,
        "response_status": 200,
        "last_attempt_at": datetime(2026, 9, 2, 9, 0, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    delivery = WebhookDelivery(**fields)
    session.add(delivery)
    await session.flush()
    return delivery


# ------------------------------------------------------------------ listing ---


async def test_endpoints_are_listed(app_client: AsyncClient, db_session: AsyncSession) -> None:
    project_id, raw_key = await _project_with_key(db_session)
    await _endpoint(db_session, project_id)

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(raw_key))

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["url"] for item in data] == ["https://hooks.example.com/seskit"]
    assert data[0]["status"] == "active"


async def test_the_signing_secret_is_never_returned(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The dashboard shows it; the API does not.

    An API response travels into logs, traces and error reports in a way a
    dashboard page does not, and nothing a caller can do here needs the secret -
    verification happens at their endpoint, with the copy they configured.
    """
    project_id, raw_key = await _project_with_key(db_session)
    await _endpoint(db_session, project_id)

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(raw_key))

    assert SECRET not in response.text
    assert "secret" not in response.text


async def test_a_disabled_endpoint_says_which_kind(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Three statuses rather than a boolean, so an integration can tell "you
    turned it off" from "SESKit gave up on it" without asking a human.
    """
    project_id, raw_key = await _project_with_key(db_session)
    await _endpoint(
        db_session, project_id, status=WebhookStatus.DISABLED_AFTER_FAILURES, failures=10
    )

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(raw_key))

    item = response.json()["data"][0]
    assert item["status"] == "disabled_after_failures"
    assert item["consecutive_failures"] == 10


async def test_no_endpoints_is_an_empty_list_not_an_error(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(raw_key))

    assert response.status_code == 200
    assert response.json()["data"] == []


# --------------------------------------------------------------- deliveries ---


async def test_deliveries_are_listed(app_client: AsyncClient, db_session: AsyncSession) -> None:
    project_id, raw_key = await _project_with_key(db_session)
    endpoint = await _endpoint(db_session, project_id)
    await _delivery(db_session, project_id, endpoint)

    response = await app_client.get(
        f"{WEBHOOKS_URL}/{endpoint.id}/deliveries", headers=_auth(raw_key)
    )

    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["status"] == "delivered"
    assert item["response_status"] == 200
    assert item["attempt_count"] == 1


async def test_a_pending_delivery_reports_when_it_is_next_due(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The delivery row is the queue as well as the log, so a caller sees the
    real state of a retry rather than a summary of it.
    """
    project_id, raw_key = await _project_with_key(db_session)
    endpoint = await _endpoint(db_session, project_id)
    await _delivery(
        db_session,
        project_id,
        endpoint,
        status=DeliveryStatus.PENDING.value,
        response_status=500,
        next_attempt_at=datetime(2026, 9, 2, 9, 5, tzinfo=UTC),
    )

    response = await app_client.get(
        f"{WEBHOOKS_URL}/{endpoint.id}/deliveries", headers=_auth(raw_key)
    )

    item = response.json()["data"][0]
    assert item["status"] == "pending"
    assert item["next_attempt_at"] is not None


# --------------------------------------------------------------- boundaries ---


async def test_another_projects_endpoints_are_not_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The tenancy boundary. The project comes from the key, so there is no
    request parameter to tamper with - this proves the scoping rather than the
    absence of a check.
    """
    project_id, mine = await _project_with_key(db_session)
    await _endpoint(db_session, project_id, url="https://mine.example.com/x")

    other_id, _ = await _project_with_key(db_session, email="them@example.com")
    await _endpoint(db_session, other_id, url="https://theirs.example.com/x")

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(mine))

    assert [item["url"] for item in response.json()["data"]] == ["https://mine.example.com/x"]
    assert "theirs.example.com" not in response.text


async def test_another_projects_deliveries_are_not_readable(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """404, and the same 404 as an id that never existed - so a caller cannot
    probe for real ids.
    """
    _, mine = await _project_with_key(db_session)
    other_id, _ = await _project_with_key(db_session, email="them@example.com")
    theirs = await _endpoint(db_session, other_id, url="https://theirs.example.com/x")

    response = await app_client.get(f"{WEBHOOKS_URL}/{theirs.id}/deliveries", headers=_auth(mine))

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_an_unknown_endpoint_gives_the_same_answer(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(
        f"{WEBHOOKS_URL}/wh_does_not_exist/deliveries", headers=_auth(raw_key)
    )

    assert response.status_code == 404


async def test_the_endpoints_need_a_key(app_client: AsyncClient, db_session: AsyncSession) -> None:
    project_id, _ = await _project_with_key(db_session)
    endpoint = await _endpoint(db_session, project_id)

    for path in (WEBHOOKS_URL, f"{WEBHOOKS_URL}/{endpoint.id}/deliveries"):
        response = await app_client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_failed"


async def test_the_endpoints_are_read_only(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Registering a destination means asking SESKit to make requests to a URL.
    That stays behind a session and a CSRF token, like API key issuance.
    """
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.post(
        WEBHOOKS_URL, headers=_auth(raw_key), json={"url": "https://x.example.com/h"}
    )

    assert response.status_code == 405


async def test_rate_limit_headers_are_present(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(WEBHOOKS_URL, headers=_auth(raw_key))

    assert int(response.headers["X-RateLimit-Limit"]) > 0
