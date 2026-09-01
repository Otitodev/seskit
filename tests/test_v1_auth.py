"""Authenticating the public API with a key, over HTTP.

The dependency and the limiter have their own unit tests; what is checked here
is that a real request carrying a real key gets through the whole stack, and
that every way of failing produces §19's envelope rather than an HTML page.
"""

from __future__ import annotations

import time

from httpx import AsyncClient
from redis.asyncio import Redis
from seskit_core.config import Settings
from seskit_core.security.api_keys import generate_key
from seskit_core.security.ratelimit import _key as _rate_limit_key
from seskit_core.security.ratelimit import _window_start
from seskit_core.services import create_api_key, create_project, register_user, revoke_api_key
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
KEYS_URL = "/v1/api-keys"


async def _project_with_key(
    session: AsyncSession, *, email: str = "owner@example.com", name: str = "production"
) -> tuple[str, str]:
    """Return ``(project_id, raw_key)``."""
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Keys")
    issued = await create_api_key(session, project_id=project.id, name=name)
    return project.id, issued.raw_key


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


# ---------------------------------------------------------------- success ---


async def test_a_valid_key_is_accepted(app_client: AsyncClient, db_session: AsyncSession) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert response.status_code == 200
    assert [key["name"] for key in response.json()["data"]] == ["production"]


async def test_the_raw_key_is_never_returned(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Shown only once" (§7) has to hold on the read path too."""
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert raw_key not in response.text
    assert "hashed_key" not in response.text
    assert response.json()["data"][0]["key_prefix"] in raw_key


# ------------------------------------------------------------- rejections ---


async def test_no_header_is_refused(app_client: AsyncClient, db_session: AsyncSession) -> None:
    await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_failed"


async def test_an_unauthenticated_api_request_gets_json_not_a_redirect(
    app_client: AsyncClient,
) -> None:
    """The distinction that matters between the two doors.

    The dashboard redirects an anonymous visitor to /login. Doing that here
    would hand an SDK an HTML login page and a confusing parse error instead of
    a 401 it can act on.
    """
    response = await app_client.get(KEYS_URL, follow_redirects=False)

    assert response.status_code == 401
    assert "application/json" in response.headers["content-type"]
    assert "<html" not in response.text.lower()


async def test_an_unknown_key_is_refused(app_client: AsyncClient, db_session: AsyncSession) -> None:
    await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers=_auth(generate_key()))

    assert response.status_code == 401


async def test_a_malformed_key_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers={"Authorization": "Bearer nonsense"})

    assert response.status_code == 401


async def test_the_wrong_scheme_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers={"Authorization": f"Basic {raw_key}"})

    assert response.status_code == 401


async def test_every_rejection_gives_the_same_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Distinguishing them would tell a caller which guesses were closer."""
    await _project_with_key(db_session)

    messages = set()
    for headers in (
        {},
        {"Authorization": "Bearer nonsense"},
        {"Authorization": f"Bearer {generate_key()}"},
    ):
        response = await app_client.get(KEYS_URL, headers=headers)
        messages.add(response.json()["error"]["message"])

    assert len(messages) == 1


async def test_a_revoked_key_stops_working_at_once(
    app_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    """Over HTTP, with the cache warmed by a real request first.

    This is the end-to-end version of the service-layer test: revocation has to
    beat the cache, because a key is revoked when it has leaked.
    """
    user = await register_user(
        db_session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(db_session, user_id=user.id, name="Keys")
    issued = await create_api_key(db_session, project_id=project.id, name="leaked")

    assert (await app_client.get(KEYS_URL, headers=_auth(issued.raw_key))).status_code == 200

    await revoke_api_key(db_session, redis_client, issued.api_key)

    assert (await app_client.get(KEYS_URL, headers=_auth(issued.raw_key))).status_code == 401


# ------------------------------------------------------------- boundaries ---


async def test_a_key_sees_only_its_own_projects_keys(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The tenancy boundary, over HTTP.

    There is no request parameter to tamper with - the project comes from the
    key - so this proves the scoping rather than the absence of a check.
    """
    _, mine = await _project_with_key(db_session, email="me@example.com", name="mine")
    await _project_with_key(db_session, email="them@example.com", name="theirs")

    response = await app_client.get(KEYS_URL, headers=_auth(mine))

    names = [key["name"] for key in response.json()["data"]]
    assert names == ["mine"]
    assert "theirs" not in response.text


# ----------------------------------------------------------- rate limiting ---


async def test_rate_limit_headers_are_present(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_with_key(db_session)

    response = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert int(response.headers["X-RateLimit-Limit"]) > 0
    assert int(response.headers["X-RateLimit-Remaining"]) >= 0
    assert int(response.headers["X-RateLimit-Reset"]) > 0


async def test_remaining_counts_down(app_client: AsyncClient, db_session: AsyncSession) -> None:
    _, raw_key = await _project_with_key(db_session)

    first = await app_client.get(KEYS_URL, headers=_auth(raw_key))
    second = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert int(second.headers["X-RateLimit-Remaining"]) < int(
        first.headers["X-RateLimit-Remaining"]
    )


async def test_going_over_the_limit_gives_429_with_retry_after(
    app_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    settings: Settings,
) -> None:
    """The allowance is spent up front rather than by making the requests.

    The limiter uses a *fixed* window keyed on the wall clock, so a loop of a
    hundred requests that happens to straddle a minute boundary resets the
    counter and the last request is allowed - which made this fail
    intermittently on a loaded machine. Seeding the counter for the current
    window puts the request under test in the same window as the allowance it
    is meant to have exhausted, and turns a hundred round trips into one.
    """
    project_id, raw_key = await _project_with_key(db_session)

    window_start = _window_start(time.time(), settings.API_RATE_LIMIT_WINDOW_SECONDS)
    await redis_client.set(
        _rate_limit_key(project_id, window_start),
        settings.API_RATE_LIMIT_PER_MINUTE,
        ex=settings.API_RATE_LIMIT_WINDOW_SECONDS,
    )

    response = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_exceeded"
    assert int(response.headers["Retry-After"]) >= 1


async def test_an_invalid_key_does_not_spend_a_projects_allowance(
    app_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """The key is verified before the limiter runs.

    Reversed, anyone could exhaust a project's quota - or every project's - by
    flooding the endpoint with garbage credentials.
    """
    _, raw_key = await _project_with_key(db_session)

    for _ in range(settings.API_RATE_LIMIT_PER_MINUTE + 10):
        await app_client.get(KEYS_URL, headers=_auth(generate_key()))

    response = await app_client.get(KEYS_URL, headers=_auth(raw_key))

    assert response.status_code == 200


# ------------------------------------------------------------- discovery ---


async def test_v1_is_in_the_openapi_schema(app_client: AsyncClient) -> None:
    """§23: the schema is for customers, and must be good enough to generate a
    client from. Dashboard routes stay out of it.
    """
    schema = (await app_client.get("/openapi.json")).json()

    assert KEYS_URL in schema["paths"]
    assert "/" not in schema["paths"]
    assert "/login" not in schema["paths"]


async def test_the_error_shape_is_documented(app_client: AsyncClient) -> None:
    """A customer should find the 401 in the spec, not by provoking one."""
    schema = (await app_client.get("/openapi.json")).json()

    responses = schema["paths"][KEYS_URL]["get"]["responses"]
    assert "401" in responses
    assert "429" in responses
