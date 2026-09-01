"""API key generation, hashing, verification, and revocation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from seskit_core.models import APIKey
from seskit_core.security.api_keys import (
    KEY_PREFIX,
    display_prefix,
    generate_key,
    hash_key,
    looks_like_key,
    parse_authorization,
)
from seskit_core.services import (
    create_api_key,
    create_project,
    get_owned_api_key,
    list_api_keys,
    register_user,
    revoke_api_key,
    touch_last_used,
    verify_api_key,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

CACHE_TTL = 60
PASSWORD = "correct-horse-battery"


# ------------------------------------------------------------- generation ---


def test_generated_keys_carry_the_prefix() -> None:
    assert generate_key().startswith(KEY_PREFIX)


def test_generated_keys_are_unique() -> None:
    """Not a strong proof, but it would catch a seeded or stubbed generator."""
    assert len({generate_key() for _ in range(1000)}) == 1000


def test_keys_are_long_enough_to_be_unguessable() -> None:
    """32 bytes via token_urlsafe is 43 characters, plus the prefix."""
    assert len(generate_key()) == len(KEY_PREFIX) + 43


def test_display_prefix_is_too_short_to_use_as_a_key() -> None:
    """It is rendered in the dashboard and returned by the API, so it must not
    be enough to authenticate with.
    """
    raw = generate_key()
    prefix = display_prefix(raw)

    assert raw.startswith(prefix)
    assert len(prefix) < len(raw) / 3


# ---------------------------------------------------------------- hashing ---


def test_hashing_is_deterministic() -> None:
    """The property Argon2 does not have, and the reason this uses SHA-256: a
    salted hash could not be looked up by value, so verification would have to
    scan every key in the table.
    """
    raw = generate_key()

    assert hash_key(raw) == hash_key(raw)


def test_different_keys_hash_differently() -> None:
    assert hash_key(generate_key()) != hash_key(generate_key())


def test_the_hash_does_not_contain_the_key() -> None:
    raw = generate_key()

    assert raw not in hash_key(raw)
    assert raw.removeprefix(KEY_PREFIX) not in hash_key(raw)


# ------------------------------------------------------- header parsing ---


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic sk_abcdefghijk",
        "sk_abcdefghijk",
        "Bearer notakey",
        "Bearer pk_abcdefghijk",
    ],
)
def test_bad_authorization_headers_are_rejected(header: str | None) -> None:
    assert parse_authorization(header) is None


def test_a_bearer_key_is_extracted() -> None:
    raw = generate_key()

    assert parse_authorization(f"Bearer {raw}") == raw


def test_the_scheme_is_case_insensitive() -> None:
    """Some HTTP clients normalise it, some do not."""
    raw = generate_key()

    assert parse_authorization(f"bearer {raw}") == raw
    assert parse_authorization(f"BEARER {raw}") == raw


def test_looks_like_key_rejects_the_bare_prefix() -> None:
    assert looks_like_key(KEY_PREFIX) is False
    assert looks_like_key(generate_key()) is True


# ------------------------------------------------------------ persistence ---


async def _project(session: AsyncSession, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Keys")
    return project.id


async def test_creating_a_key_stores_only_the_hash(db_session: AsyncSession) -> None:
    """The guarantee behind "shown only once" (§7)."""
    project_id = await _project(db_session)

    issued = await create_api_key(db_session, project_id=project_id, name="production")

    stored = await db_session.scalar(select(APIKey).where(APIKey.id == issued.api_key.id))
    assert stored is not None
    assert stored.hashed_key == hash_key(issued.raw_key)
    assert issued.raw_key not in stored.hashed_key
    assert issued.raw_key != stored.key_prefix
    # No column anywhere holds the raw value.
    assert issued.raw_key not in str(stored.__dict__)


async def test_a_new_key_verifies(db_session: AsyncSession, redis_client: Redis) -> None:
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    resolved = await verify_api_key(
        db_session, redis_client, raw_key=issued.raw_key, cache_ttl_seconds=CACHE_TTL
    )

    assert resolved == project_id


async def test_an_unknown_key_does_not_verify(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await _project(db_session)

    resolved = await verify_api_key(
        db_session, redis_client, raw_key=generate_key(), cache_ttl_seconds=CACHE_TTL
    )

    assert resolved is None


async def test_unknown_keys_are_not_cached(db_session: AsyncSession, redis_client: Redis) -> None:
    """Otherwise anyone could fill Redis by presenting made-up keys."""
    await _project(db_session)
    before = len(await redis_client.keys("apikey:*"))

    await verify_api_key(
        db_session, redis_client, raw_key=generate_key(), cache_ttl_seconds=CACHE_TTL
    )

    assert len(await redis_client.keys("apikey:*")) == before


async def test_a_revoked_key_does_not_verify(db_session: AsyncSession, redis_client: Redis) -> None:
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await revoke_api_key(db_session, redis_client, issued.api_key)

    assert (
        await verify_api_key(
            db_session, redis_client, raw_key=issued.raw_key, cache_ttl_seconds=CACHE_TTL
        )
        is None
    )


async def test_revocation_takes_effect_immediately_not_after_the_cache_expires(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """The one that matters.

    A key is usually revoked *because* it leaked. If revocation only took hold
    when the cache entry expired, the key would keep working for the length of
    the TTL - precisely the window in which the damage happens. The TTL here is
    an hour, so a test that passed by waiting it out would prove nothing.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="leaked")

    # Warm the cache, as a real request would.
    assert (
        await verify_api_key(
            db_session, redis_client, raw_key=issued.raw_key, cache_ttl_seconds=3600
        )
        == project_id
    )

    await revoke_api_key(db_session, redis_client, issued.api_key)

    assert (
        await verify_api_key(
            db_session, redis_client, raw_key=issued.raw_key, cache_ttl_seconds=3600
        )
        is None
    )


async def test_revoking_twice_keeps_the_first_timestamp(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Revocation records when the key was withdrawn; a second call must not
    rewrite that to a later, misleading time.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await revoke_api_key(db_session, redis_client, issued.api_key)
    first = issued.api_key.revoked_at
    await revoke_api_key(db_session, redis_client, issued.api_key)

    assert issued.api_key.revoked_at == first


async def test_the_cache_is_used_on_the_second_lookup(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await verify_api_key(
        db_session, redis_client, raw_key=issued.raw_key, cache_ttl_seconds=CACHE_TTL
    )

    assert await redis_client.get(f"apikey:{hash_key(issued.raw_key)}") == project_id


# ------------------------------------------------------------- last used ---


async def test_last_used_is_recorded(db_session: AsyncSession, redis_client: Redis) -> None:
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")
    assert issued.api_key.last_used_at is None

    await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)

    assert issued.api_key.last_used_at is not None


async def test_last_used_is_not_written_on_every_request(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Writing per request would add a database write to every API call, for a
    column read a few times a day.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)
    first = issued.api_key.last_used_at

    for _ in range(5):
        await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)

    assert issued.api_key.last_used_at == first


async def test_last_used_writes_again_once_the_interval_passes(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)
    assert issued.api_key.last_used_at is not None

    # Wind the stored value back rather than comparing two consecutive now()
    # calls. `datetime.now()` resolution is coarse enough on some platforms -
    # around 16ms on Windows - that two writes in quick succession produce the
    # same timestamp, which made this assertion a coin flip rather than a test.
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    issued.api_key.last_used_at = stale
    await db_session.flush()

    # Drop the marker rather than sleeping: the behaviour under test is "writes
    # again once the marker is gone", and a real interval would be a slow test.
    await redis_client.delete(f"apikey_used:{issued.api_key.id}")
    await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)

    assert issued.api_key.last_used_at > stale


# ------------------------------------------------------- project scoping ---


async def test_keys_are_listed_newest_first(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    await create_api_key(db_session, project_id=project_id, name="first")
    await create_api_key(db_session, project_id=project_id, name="second")

    assert [key.name for key in await list_api_keys(db_session, project_id)] == [
        "second",
        "first",
    ]


async def test_revoked_keys_stay_in_the_list(db_session: AsyncSession, redis_client: Redis) -> None:
    """A key that disappears on revocation leaves no record it ever existed."""
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="production")

    await revoke_api_key(db_session, redis_client, issued.api_key)

    keys = await list_api_keys(db_session, project_id)
    assert [key.name for key in keys] == ["production"]
    assert keys[0].is_active is False


async def test_a_key_is_not_visible_from_another_project(db_session: AsyncSession) -> None:
    """The tenancy boundary, at the service layer."""
    mine = await _project(db_session, "owner@example.com")
    theirs = await _project(db_session, "other@example.com")
    issued = await create_api_key(db_session, project_id=theirs, name="theirs")

    assert await get_owned_api_key(db_session, key_id=issued.api_key.id, project_id=mine) is None
    assert (
        await get_owned_api_key(db_session, key_id=issued.api_key.id, project_id=theirs) is not None
    )
    assert await list_api_keys(db_session, mine) == []


async def test_deleting_a_project_takes_its_keys(db_session: AsyncSession) -> None:
    """Cascade in the database, so a key can never outlive the project it
    authenticates for.
    """
    project_id = await _project(db_session)
    await create_api_key(db_session, project_id=project_id, name="production")

    from seskit_core.models import Project

    project = await db_session.get(Project, project_id)
    assert project is not None
    await db_session.delete(project)
    await db_session.flush()

    remaining = await db_session.scalars(select(APIKey).where(APIKey.project_id == project_id))
    assert list(remaining) == []


# ------------------------------------------------------- the dashboard page ---


async def _sign_in(client: AsyncClient, email: str = "owner@example.com") -> None:
    response = await client.post(
        "/signup", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


async def test_the_page_needs_a_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/api-keys", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_a_fresh_project_has_no_keys(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.get("/api-keys")

    assert response.status_code == 200
    assert "No API keys yet" in response.text


async def test_creating_a_key_shows_it_once(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)

    created = await app_client.post("/api-keys", data={"csrf_token": csrf, "name": "production"})

    assert created.status_code == 200
    assert "production" in created.text
    # The raw key is on the page exactly once, in the reveal panel.
    body = created.text
    start = body.index(KEY_PREFIX)
    raw = body[start : start + 46]
    assert raw.startswith(KEY_PREFIX)

    # ...and never again.
    assert raw not in (await app_client.get("/api-keys")).text


async def test_the_stored_key_is_not_rendered(app_client: AsyncClient) -> None:
    """The list page must show the prefix, never anything usable."""
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)
    await app_client.post("/api-keys", data={"csrf_token": csrf, "name": "production"})

    page = (await app_client.get("/api-keys")).text

    assert "hashed_key" not in page
    assert "production" in page


async def test_an_unnamed_key_still_gets_a_name(app_client: AsyncClient) -> None:
    """Every unnamed key looks alike in the list, which defeats naming them."""
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)

    created = await app_client.post("/api-keys", data={"csrf_token": csrf, "name": "   "})

    assert "Untitled key" in created.text


async def test_creating_a_key_without_csrf_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    response = await app_client.post("/api-keys", data={"name": "production"})

    assert response.status_code == 403


async def test_revoking_marks_the_key_and_keeps_it_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)
    await app_client.post("/api-keys", data={"csrf_token": csrf, "name": "production"})

    key = (await db_session.scalars(select(APIKey))).one()
    response = await app_client.post(f"/api-keys/{key.id}/revoke", data={"csrf_token": csrf})

    assert response.status_code == 200
    assert "Revoked" in response.text
    assert "production" in response.text


async def test_revoking_without_csrf_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)
    await app_client.post("/api-keys", data={"csrf_token": csrf, "name": "production"})
    key = (await db_session.scalars(select(APIKey))).one()

    response = await app_client.post(f"/api-keys/{key.id}/revoke", data={})

    assert response.status_code == 403


async def test_revoking_an_unknown_key_does_not_error(app_client: AsyncClient) -> None:
    """Already revoked, or never existed - either way the page shows the truth."""
    await _sign_in(app_client)
    csrf = _csrf((await app_client.get("/api-keys")).text)

    response = await app_client.post(
        "/api-keys/key_01DOESNOTEXIST/revoke", data={"csrf_token": csrf}
    )

    assert response.status_code == 200


async def test_you_cannot_revoke_another_projects_key(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The tenancy boundary on the dashboard side.

    Ownership is part of the lookup, so a key id belonging to someone else
    resolves to nothing rather than to their key.
    """
    victim = await register_user(
        db_session, email="victim@example.com", password=PASSWORD, allow_signup=True
    )
    victim_project = await create_project(db_session, user_id=victim.id, name="Theirs")
    theirs = await create_api_key(db_session, project_id=victim_project.id, name="theirs")

    await register_user(
        db_session, email="intruder@example.com", password=PASSWORD, allow_signup=True
    )
    await app_client.post(
        "/login",
        data={"email": "intruder@example.com", "password": PASSWORD},
        follow_redirects=False,
    )
    csrf = _csrf((await app_client.get("/api-keys")).text)

    response = await app_client.post(
        f"/api-keys/{theirs.api_key.id}/revoke", data={"csrf_token": csrf}
    )

    assert response.status_code == 200
    # The decisive check: the key was not touched, and never appears.
    await db_session.refresh(theirs.api_key)
    assert theirs.api_key.revoked_at is None
    assert "theirs" not in response.text


# ------------------------------------------------------------ degradation ---
#
# Redis is a cache on this path, never the source of truth. Losing it should
# cost latency, not availability - the database can answer every question the
# cache is asked.


class BrokenRedis:
    """Raises on every operation, like a Redis that has gone away."""

    async def get(self, *args: object, **kwargs: object) -> object:
        raise RedisConnectionError("redis is down")

    async def set(self, *args: object, **kwargs: object) -> object:
        raise RedisConnectionError("redis is down")

    async def delete(self, *args: object, **kwargs: object) -> object:
        raise RedisConnectionError("redis is down")


async def test_a_valid_key_still_verifies_without_redis(db_session: AsyncSession) -> None:
    """The row in Postgres is the authoritative answer. Rejecting a valid key
    because the cache is unreachable would turn a Redis outage into an
    authentication outage.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="prod")

    resolved = await verify_api_key(
        db_session,
        BrokenRedis(),  # type: ignore[arg-type]
        raw_key=issued.raw_key,
        cache_ttl_seconds=60,
    )

    assert resolved == project_id


async def test_an_unknown_key_is_still_refused_without_redis(
    db_session: AsyncSession,
) -> None:
    """Degrading must not become failing open on authentication - that is the
    opposite trade from the rate limiter, and deliberately so.
    """
    await _project(db_session)

    resolved = await verify_api_key(
        db_session,
        BrokenRedis(),  # type: ignore[arg-type]
        raw_key=generate_key(),
        cache_ttl_seconds=60,
    )

    assert resolved is None


async def test_revocation_persists_even_if_the_cache_cannot_be_evicted(
    db_session: AsyncSession,
) -> None:
    """The database write is what makes a revocation real. Raising on the failed
    eviction would abandon it altogether; the short cache TTL is the backstop.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="leaked")

    await revoke_api_key(db_session, BrokenRedis(), issued.api_key)  # type: ignore[arg-type]

    assert issued.api_key.revoked_at is not None
