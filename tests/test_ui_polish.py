"""Action feedback (§17, `docs/design-system.md`).

Until this, every state-changing form in the dashboard re-rendered its page and
said nothing. A successful save looked exactly like a save that never happened,
which is the failure mode that makes a user press the button again.

The test that matters here is the escaping one. Two confirmations quote input
the user typed - an identity value and an API key name - so the flash is a path
from a form field to rendered HTML, and it needs holding to the same standard
as the email body on the Emails page.
"""

from __future__ import annotations

from httpx import AsyncClient
from seskit_core.models import APIKey, WebhookEndpoint
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"


async def _sign_in(client: AsyncClient) -> None:
    response = await client.post(
        "/signup",
        data={"email": "owner@example.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


async def _csrf(client: AsyncClient, path: str) -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# ------------------------------------------------------------------ toast ---


async def test_a_page_view_says_nothing(app_client: AsyncClient) -> None:
    """A toast belongs to an action. Arriving on a page is not one, and a
    confirmation with nothing behind it teaches people to ignore the next one.
    """
    await _sign_in(app_client)

    page = await app_client.get("/webhooks")

    assert "data-toast" not in page.text


async def test_an_action_confirms_itself(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(app_client, "/webhooks"),
        },
    )

    assert response.status_code == 200
    assert "data-toast" in response.text
    assert "Endpoint added." in response.text


async def test_a_refused_action_does_not_confirm(app_client: AsyncClient) -> None:
    """The refusal is the message. A toast beside it would be two answers to
    one question, and the reassuring one is the wrong one.
    """
    await _sign_in(app_client)

    response = await app_client.post(
        "/webhooks",
        data={"url": "http://127.0.0.1/hook", "csrf_token": await _csrf(app_client, "/webhooks")},
    )

    assert response.status_code == 400
    assert "data-toast" not in response.text


async def test_the_toast_announces_itself_without_stealing_focus(
    app_client: AsyncClient,
) -> None:
    """`role="status"` is announced politely. `alert` would interrupt, and the
    user is usually still on the control they just pressed.
    """
    await _sign_in(app_client)

    response = await app_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(app_client, "/webhooks"),
        },
    )

    assert 'role="status"' in response.text
    assert 'aria-live="polite"' in response.text


async def test_the_message_is_in_the_html_not_built_by_javascript(
    app_client: AsyncClient,
) -> None:
    """It is rendered server-side and floated afterwards, so it survives a page
    that loads with scripts blocked rather than existing only if they arrive.
    """
    await _sign_in(app_client)

    response = await app_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(app_client, "/webhooks"),
        },
    )

    # Present in the markup, and not yet floating - app.js adds that attribute.
    assert "Endpoint added." in response.text
    assert "data-toast-floating" not in response.text


# --------------------------------------------------------------- escaping ---


async def test_a_confirmation_quoting_the_user_escapes_it(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The API key name reaches the confirmation, so it reaches the page.

    Same reasoning as the HTML body on the Emails page: the value is chosen by
    someone with dashboard access, and rendering it unescaped would run their
    markup inside an authenticated session.
    """
    await _sign_in(app_client)
    payload = '<script>alert("xss")</script>'

    created = await app_client.post(
        "/api-keys",
        data={"name": payload, "csrf_token": await _csrf(app_client, "/api-keys")},
    )
    assert created.status_code == 200

    key_id = await db_session.scalar(select(APIKey.id))
    assert key_id is not None

    revoked = await app_client.post(
        f"/api-keys/{key_id}/revoke",
        data={"csrf_token": await _csrf(app_client, "/api-keys")},
    )

    assert "Revoked" in revoked.text
    assert payload not in revoked.text
    assert "&lt;script&gt;" in revoked.text


# ------------------------------------------------------------- honest text ---


async def test_an_unchanged_url_does_not_claim_to_have_changed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Resubmitting the same address is the case where a user is checking
    carefully that what they typed is what is stored. Saying "changed" there is
    a small lie in the one place it would be noticed.
    """
    await _sign_in(app_client)
    url = "https://hooks.example.com/seskit"

    await app_client.post(
        "/webhooks", data={"url": url, "csrf_token": await _csrf(app_client, "/webhooks")}
    )

    endpoint_id = await db_session.scalar(select(WebhookEndpoint.id))
    assert endpoint_id is not None

    response = await app_client.post(
        f"/webhooks/{endpoint_id}/url",
        data={"url": url, "csrf_token": await _csrf(app_client, "/webhooks")},
    )

    assert "already the endpoint's URL" in response.text
    assert "Endpoint URL changed." not in response.text


async def test_pausing_and_resuming_say_which_one_happened(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)

    await app_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(app_client, "/webhooks"),
        },
    )

    endpoint_id = await db_session.scalar(select(WebhookEndpoint.id))
    assert endpoint_id is not None

    paused = await app_client.post(
        f"/webhooks/{endpoint_id}/enabled",
        data={"enabled": "", "csrf_token": await _csrf(app_client, "/webhooks")},
    )
    assert "Endpoint paused." in paused.text

    resumed = await app_client.post(
        f"/webhooks/{endpoint_id}/enabled",
        data={"enabled": "on", "csrf_token": await _csrf(app_client, "/webhooks")},
    )
    assert "Endpoint enabled." in resumed.text


# ----------------------------------------------------------- the skip link ---


async def test_the_skip_link_can_be_seen_when_focused(app_client: AsyncClient) -> None:
    """It was in the DOM already, wearing `.visually-hidden` - which has no
    `:focus` escape, so a sighted keyboard user tabbed to it and saw nothing.

    `.visually-hidden` must not grow one either: it is also what hides the
    "View " prefix inside the Emails table links.
    """
    await _sign_in(app_client)

    page = await app_client.get("/")

    assert 'class="skip-link" href="#main"' in page.text
    assert 'id="main"' in page.text
