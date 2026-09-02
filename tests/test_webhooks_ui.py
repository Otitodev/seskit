"""The Webhooks dashboard page (§17).

What is worth testing here is what a user is told and what they are stopped
from doing, rather than that a template renders.

Two things carry weight. Registering an endpoint is the one place a user hands
SESKit a URL and asks it to make requests, so the refusal has to reach the form
rather than a log. And an endpoint SESKit switched off has to say so - a switch
that appears to have moved on its own, with nothing explaining why, is the
version of this that generates support questions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from seskit_core.models import (
    DeliveryStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    Project,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookStatus,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

URL = "https://hooks.example.com/seskit"


async def _csrf(client: AsyncClient, path: str = "/webhooks") -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _endpoint(
    session: AsyncSession,
    *,
    url: str = URL,
    status: WebhookStatus = WebhookStatus.ACTIVE,
    failures: int = 0,
) -> WebhookEndpoint:
    project_id = await session.scalar(select(Project.id))
    endpoint = WebhookEndpoint(
        project_id=project_id,
        url=url,
        secret="whsec_visible_on_purpose",
        status=status.value,
        consecutive_failures=failures,
    )
    session.add(endpoint)
    await session.flush()
    return endpoint


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0)


# ------------------------------------------------------------------- empty ---


async def test_the_empty_page_explains_what_a_webhook_is_for(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The first thing a new install shows, so it says what the feature does
    rather than only that there is nothing here.
    """
    page = await signed_in_client.get("/webhooks")

    assert page.status_code == 200
    assert "No webhook endpoints yet" in page.text
    assert "without polling" in page.text


# -------------------------------------------------------------- registering ---


async def test_an_endpoint_can_be_registered(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _csrf(signed_in_client)

    page = await signed_in_client.post("/webhooks", data={"csrf_token": token, "url": URL})

    assert page.status_code == 200
    assert await _count(db_session) == 1
    assert URL in page.text


async def test_a_refused_url_is_explained_on_the_form(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The courtesy half of the destination check. The control runs again at
    delivery, against the resolved address - see security/destinations.py.

    The test environment is local, where private addresses are permitted, so
    this uses a scheme that is refused in every environment.
    """
    token = await _csrf(signed_in_client)

    page = await signed_in_client.post(
        "/webhooks", data={"csrf_token": token, "url": "ftp://example.com/x"}
    )

    assert page.status_code == 400
    assert "cannot be used for webhooks" in page.text
    assert await _count(db_session) == 0


async def test_registering_needs_a_csrf_token(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A form, not a link. Asking SESKit to start making requests to a URL is
    not something another site should be able to trigger with an image tag.
    """
    page = await signed_in_client.post("/webhooks", data={"csrf_token": "forged", "url": URL})

    assert page.status_code == 403
    assert await _count(db_session) == 0


# ------------------------------------------------------------------ secret ---


async def test_the_signing_secret_is_shown(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deliberately unlike an API key, which is shown once and then only
    hashed. The customer needs this one to verify signatures, every time.
    """
    endpoint = await _endpoint(db_session)

    page = await signed_in_client.get("/webhooks")

    assert endpoint.secret in page.text


async def test_the_page_shows_how_to_verify_a_signature(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A signature scheme a customer cannot reimplement is one they will skip
    verifying, so the instructions sit beside the secret rather than only in the
    README.
    """
    await _endpoint(db_session)

    page = await signed_in_client.get("/webhooks")

    assert "X-SESKit-Signature" in page.text
    assert "compare_digest" in page.text


async def test_the_secret_can_be_rotated(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    endpoint = await _endpoint(db_session)
    original = endpoint.secret
    token = await _csrf(signed_in_client)

    await signed_in_client.post(f"/webhooks/{endpoint.id}/secret", data={"csrf_token": token})

    await db_session.refresh(endpoint)
    assert endpoint.secret != original


async def test_the_documented_snippet_actually_verifies(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The instructions on the page are executed, not proofread.

    This runs the exact text shown to the user against a signature produced by
    the real signing code. If the scheme ever changes without the snippet
    changing with it, this fails - which is the only way documentation stays
    true. Phase 6 learned the same lesson with a curl example that did not run.
    """
    from seskit_api.routes.webhooks import VERIFY_SNIPPET
    from seskit_core.security.webhooks import sign

    namespace: dict[str, Any] = {}
    exec(VERIFY_SNIPPET, namespace)  # noqa: S102 - the point is to run the docs
    verify_as_a_customer_would: Any = namespace["verify"]

    secret = "whsec_documented"
    body = b'{"id":"evt_1","type":"email.delivered"}'
    signature, timestamp = sign(secret, body)

    assert verify_as_a_customer_would(secret, body, signature, str(timestamp)) is True
    # And it refuses a body that was altered after signing.
    assert verify_as_a_customer_would(secret, b"{}", signature, str(timestamp)) is False


# ----------------------------------------------------------------- enabling ---


async def test_an_endpoint_can_be_disabled_and_re_enabled(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    endpoint = await _endpoint(db_session)
    token = await _csrf(signed_in_client)

    await signed_in_client.post(
        f"/webhooks/{endpoint.id}/enabled", data={"csrf_token": token, "enabled": "off"}
    )
    await db_session.refresh(endpoint)
    assert endpoint.is_enabled is False

    await signed_in_client.post(
        f"/webhooks/{endpoint.id}/enabled", data={"csrf_token": token, "enabled": "on"}
    )
    await db_session.refresh(endpoint)
    assert endpoint.is_enabled is True


async def test_the_toggle_posts_the_opposite_of_the_current_state(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Get this backwards and the button looks right and does nothing."""
    endpoint = await _endpoint(db_session)

    active = await signed_in_client.get("/webhooks")
    assert 'value="off"' in active.text

    endpoint.status = WebhookStatus.DISABLED_BY_USER.value
    await db_session.flush()

    disabled = await signed_in_client.get("/webhooks")
    assert 'value="on"' in disabled.text


async def test_an_auto_disabled_endpoint_says_why(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reason this status exists at all. "Disabled" alone would look like
    something the user did.
    """
    await _endpoint(db_session, status=WebhookStatus.DISABLED_AFTER_FAILURES, failures=10)

    page = await signed_in_client.get("/webhooks")

    assert "SESKit stopped sending to this endpoint" in page.text
    assert "failed 10 times in a row" in page.text


async def test_re_enabling_clears_the_failure_count(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A user who has fixed their endpoint gets a full allowance, not one
    attempt before it switches off again.
    """
    endpoint = await _endpoint(
        db_session, status=WebhookStatus.DISABLED_AFTER_FAILURES, failures=10
    )
    token = await _csrf(signed_in_client)

    await signed_in_client.post(
        f"/webhooks/{endpoint.id}/enabled", data={"csrf_token": token, "enabled": "on"}
    )

    await db_session.refresh(endpoint)
    assert endpoint.consecutive_failures == 0
    assert endpoint.is_enabled is True


# ------------------------------------------------------------------ history ---


async def test_delivery_history_is_shown(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    endpoint = await _endpoint(db_session)
    project_id = await db_session.scalar(select(Project.id))
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
    db_session.add(email)
    await db_session.flush()
    event = EmailEvent(
        email_id=email.id,
        event_type=EventType.BOUNCED.value,
        provider_event_id="sns-1",
        occurred_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        payload={"type": "email.bounced", "data": {}},
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        WebhookDelivery(
            webhook_endpoint_id=endpoint.id,
            event_id=event.id,
            status=DeliveryStatus.FAILED.value,
            attempt_count=6,
            response_status=500,
            last_attempt_at=datetime(2026, 9, 2, 9, 5, tzinfo=UTC),
        )
    )
    await db_session.flush()

    page = await signed_in_client.get("/webhooks")

    assert "Bounced" in page.text
    assert "Failed 500" in page.text


async def test_an_endpoint_with_no_deliveries_points_at_event_setup(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The likeliest reason a new endpoint sees nothing: SES publishes no events
    at all until event reporting is set up, so "nothing yet" would be a dead end.
    """
    await _endpoint(db_session)

    page = await signed_in_client.get("/webhooks")

    assert "Nothing sent yet" in page.text
    assert 'href="/aws"' in page.text


# --------------------------------------------------------------- boundaries ---


async def test_an_endpoint_can_be_deleted(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    endpoint = await _endpoint(db_session)
    token = await _csrf(signed_in_client)

    await signed_in_client.post(f"/webhooks/{endpoint.id}/delete", data={"csrf_token": token})

    assert await _count(db_session) == 0


async def test_another_projects_endpoint_is_not_reachable(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ownership is part of the query, so an id from another project resolves
    to nothing rather than to someone else's endpoint.
    """
    from seskit_core.services import create_project, register_user

    stranger = await register_user(
        db_session, email="them@example.com", password="correct-horse-battery", allow_signup=True
    )
    other = await create_project(db_session, user_id=stranger.id, name="Theirs")
    theirs = WebhookEndpoint(
        project_id=other.id, url="https://theirs.example.com/x", secret="whsec_theirs"
    )
    db_session.add(theirs)
    await db_session.flush()
    token = await _csrf(signed_in_client)

    await signed_in_client.post(f"/webhooks/{theirs.id}/delete", data={"csrf_token": token})

    # Still there, and never shown.
    await db_session.refresh(theirs)
    assert theirs.id
    page = await signed_in_client.get("/webhooks")
    assert "theirs.example.com" not in page.text
