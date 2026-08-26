"""API key generation, hashing, verification, and revocation."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
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
    first = issued.api_key.last_used_at

    # Drop the marker rather than sleeping: the behaviour under test is "writes
    # again once the marker is gone", and a real interval would be a slow test.
    await redis_client.delete(f"apikey_used:{issued.api_key.id}")
    await touch_last_used(db_session, redis_client, raw_key=issued.raw_key, interval_seconds=60)

    assert issued.api_key.last_used_at != first


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
