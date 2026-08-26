"""Signup, login, logout, and project selection over HTTP."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from seskit_core.security.sessions import read_session
from seskit_core.services import list_projects, register_user
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
COOKIE = "seskit_session"


async def _sign_in(client: AsyncClient, email: str = "owner@example.com") -> None:
    """Register through the app so the client ends up holding a real cookie."""
    response = await client.post(
        "/signup", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


# --------------------------------------------------------- access control ---


async def test_anonymous_dashboard_redirects_to_login(app_client: AsyncClient) -> None:
    """A browser must land on the login page, not a bare 401."""
    response = await app_client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_page_is_public(app_client: AsyncClient) -> None:
    assert (await app_client.get("/login")).status_code == 200


async def test_status_partial_stays_public(app_client: AsyncClient) -> None:
    """It reports only dependency health, which /readyz already exposes."""
    assert (await app_client.get("/partials/status")).status_code == 200


# ------------------------------------------------------------------ signup ---


async def test_signup_is_open_on_a_fresh_install(app_client: AsyncClient) -> None:
    response = await app_client.get("/signup")

    assert response.status_code == 200
    assert "Create the owner account" in response.text


async def test_signup_creates_an_owner_with_a_project(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)

    from seskit_core.services import get_user_by_email

    user = await get_user_by_email(db_session, "owner@example.com")
    assert user is not None
    assert user.is_owner is True
    assert len(await list_projects(db_session, user.id)) == 1


async def test_signup_signs_you_in(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    assert (await app_client.get("/", follow_redirects=False)).status_code == 200


async def test_signup_closes_after_the_first_account(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    app_client.cookies.clear()

    response = await app_client.get("/signup")

    assert response.status_code == 200
    assert "Registration is closed" in response.text


async def test_second_signup_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    app_client.cookies.clear()

    response = await app_client.post(
        "/signup",
        data={"email": "stranger@example.com", "password": PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "closed" in response.text.lower()


async def test_short_password_is_refused(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/signup", data={"email": "owner@example.com", "password": "short"}
    )

    assert response.status_code == 400
    assert "at least" in response.text.lower()


async def test_signed_in_user_is_sent_away_from_signup(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.get("/signup", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ------------------------------------------------------------------- login ---


async def test_correct_credentials_sign_in(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    response = await app_client.post(
        "/login",
        data={"email": "owner@example.com", "password": PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert app_client.cookies.get(COOKIE)


async def test_wrong_password_and_unknown_email_give_the_same_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The anti-enumeration guarantee.

    If these differed, the login form would tell anyone which addresses have
    accounts here.
    """
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    wrong_password = await app_client.post(
        "/login", data={"email": "owner@example.com", "password": "not-it"}
    )
    unknown_email = await app_client.post(
        "/login", data={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 400
    assert "Email or password is incorrect." in wrong_password.text
    assert "Email or password is incorrect." in unknown_email.text


async def test_failed_login_sets_no_cookie(app_client: AsyncClient) -> None:
    await app_client.post("/login", data={"email": "nobody@example.com", "password": PASSWORD})

    assert app_client.cookies.get(COOKIE) is None


async def test_session_cookie_is_defended(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """HttpOnly stops XSS lifting it; SameSite stops cross-site POSTs using it."""
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    response = await app_client.post(
        "/login",
        data={"email": "owner@example.com", "password": PASSWORD},
        follow_redirects=False,
    )

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


async def test_login_issues_a_new_session_token(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Session fixation: a pre-login token must never survive the login."""
    await register_user(db_session, email="owner@example.com", password=PASSWORD)
    credentials = {"email": "owner@example.com", "password": PASSWORD}

    await app_client.post("/login", data=credentials, follow_redirects=False)
    first = app_client.cookies.get(COOKIE)

    app_client.cookies.clear()
    await app_client.post("/login", data=credentials, follow_redirects=False)
    second = app_client.cookies.get(COOKIE)

    assert first != second


async def test_login_throttles_after_repeated_failures(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)
    attempt = {"email": "owner@example.com", "password": "wrong"}

    for _ in range(10):
        await app_client.post("/login", data=attempt)

    response = await app_client.post("/login", data=attempt)

    assert response.status_code == 429
    assert "too many" in response.text.lower()


async def test_throttled_user_cannot_get_in_with_the_right_password(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The limiter has to hold even once the attacker guesses correctly."""
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    for _ in range(10):
        await app_client.post("/login", data={"email": "owner@example.com", "password": "wrong"})

    response = await app_client.post(
        "/login",
        data={"email": "owner@example.com", "password": PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 429


# ------------------------------------------------------------------ logout ---


async def test_logout_ends_the_session_server_side(app_client: AsyncClient, redis_client) -> None:  # type: ignore[no-untyped-def]
    """Clearing the cookie is not enough - the record itself must go."""
    await _sign_in(app_client)
    token = app_client.cookies.get(COOKIE)
    assert token

    page = await app_client.get("/")
    csrf = _extract_csrf(page.text)

    response = await app_client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert await read_session(redis_client, token, 3600) is None


async def test_logout_without_a_csrf_token_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.post("/logout", data={}, follow_redirects=False)

    assert response.status_code == 403


async def test_logout_with_a_wrong_csrf_token_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.post(
        "/logout", data={"csrf_token": "forged"}, follow_redirects=False
    )

    assert response.status_code == 403


async def test_dashboard_is_unreachable_after_logout(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    csrf = _extract_csrf((await app_client.get("/")).text)
    await app_client.post("/logout", data={"csrf_token": csrf})

    response = await app_client.get("/", follow_redirects=False)

    assert response.status_code == 303


# -------------------------------------------------------- project switching ---


async def test_switching_to_your_own_project_works(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    from seskit_core.services import create_project, get_user_by_email

    user = await get_user_by_email(db_session, "owner@example.com")
    assert user is not None
    staging = await create_project(db_session, user_id=user.id, name="Staging")

    csrf = _extract_csrf((await app_client.get("/")).text)
    response = await app_client.post(
        "/projects/switch",
        data={"csrf_token": csrf, "project_id": staging.id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Staging" in (await app_client.get("/")).text


async def test_switching_to_someone_elses_project_is_denied(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The tenancy boundary, over HTTP.

    Without the ownership check, posting any project id would point the
    dashboard at another customer's data.
    """
    from seskit_core.services import create_project

    victim = await register_user(db_session, email="victim@example.com", password=PASSWORD)
    secret = await create_project(db_session, user_id=victim.id, name="VictimSecretProject")

    # Registered directly rather than through /signup, which is closed once the
    # victim exists.
    await register_user(
        db_session, email="intruder@example.com", password=PASSWORD, allow_signup=True
    )
    await app_client.post(
        "/login",
        data={"email": "intruder@example.com", "password": PASSWORD},
        follow_redirects=False,
    )

    csrf = _extract_csrf((await app_client.get("/")).text)
    response = await app_client.post(
        "/projects/switch",
        data={"csrf_token": csrf, "project_id": secret.id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    # The decisive check: the victim's project never appears on the intruder's
    # dashboard, before or after the attempt.
    assert "VictimSecretProject" not in (await app_client.get("/")).text


async def test_switching_without_a_csrf_token_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.post(
        "/projects/switch", data={"project_id": "proj_x"}, follow_redirects=False
    )

    assert response.status_code == 403


# ------------------------------------------------------------------ helpers ---


def _extract_csrf(html: str) -> str:
    """Pull the CSRF token out of a rendered page.

    Reading it from the HTML rather than the session store keeps these tests
    honest: if the token stops being rendered, the tests fail rather than
    silently passing on a value the browser never sees.
    """
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_extract_csrf_finds_the_field() -> None:
    assert _extract_csrf('<input name="csrf_token" value="abc123">') == "abc123"


def test_extract_csrf_fails_loudly_when_absent() -> None:
    with pytest.raises(ValueError):
        _extract_csrf("<form></form>")
