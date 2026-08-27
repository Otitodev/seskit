"""Session storage, against a real Redis.

Expiry, set membership, and pipeline behaviour are Redis semantics; a mock would
only assert that the code calls the functions it calls.
"""

from __future__ import annotations

from redis.asyncio import Redis
from seskit_core.redis import smembers
from seskit_core.security.csrf import generate_csrf_token, tokens_match
from seskit_core.security.sessions import (
    create_session,
    delete_session,
    delete_user_sessions,
    generate_token,
    read_session,
)

TTL = 3600


# ----------------------------------------------------------------- tokens ---


def test_tokens_are_unique() -> None:
    assert len({generate_token() for _ in range(500)}) == 500


def test_token_is_long_enough_to_be_unguessable() -> None:
    # 32 random bytes, url-safe base64 encoded.
    assert len(generate_token()) >= 43


# ---------------------------------------------------------------- storage ---


async def test_created_session_can_be_read_back(redis_client: Redis) -> None:
    token, data = await create_session(redis_client, "usr_1", TTL)

    loaded = await read_session(redis_client, token, TTL)

    assert loaded is not None
    assert loaded.user_id == "usr_1"
    assert loaded.csrf_token == data.csrf_token


async def test_each_session_gets_its_own_csrf_token(redis_client: Redis) -> None:
    _, first = await create_session(redis_client, "usr_1", TTL)
    _, second = await create_session(redis_client, "usr_1", TTL)

    assert first.csrf_token != second.csrf_token


async def test_login_produces_a_new_token(redis_client: Redis) -> None:
    """Session fixation: a token must never be carried across a login.

    Otherwise an attacker who plants a known token in the victim's browser
    inherits the session the moment the victim signs in.
    """
    first, _ = await create_session(redis_client, "usr_1", TTL)
    second, _ = await create_session(redis_client, "usr_1", TTL)

    assert first != second


async def test_unknown_token_reads_as_none(redis_client: Redis) -> None:
    assert await read_session(redis_client, "not-a-real-token", TTL) is None


async def test_empty_token_reads_as_none(redis_client: Redis) -> None:
    """A browser with no cookie sends nothing; that must not hit Redis."""
    assert await read_session(redis_client, "", TTL) is None


async def test_session_carries_an_expiry(redis_client: Redis) -> None:
    token, _ = await create_session(redis_client, "usr_1", TTL)

    assert 0 < await redis_client.ttl(f"session:{token}") <= TTL


async def test_reading_a_session_extends_its_expiry(redis_client: Redis) -> None:
    """The timeout is idle, not absolute - an active user stays signed in."""
    token, _ = await create_session(redis_client, "usr_1", 100)
    await redis_client.expire(f"session:{token}", 10)

    await read_session(redis_client, token, TTL)

    assert await redis_client.ttl(f"session:{token}") > 10


async def test_expired_session_is_gone(redis_client: Redis) -> None:
    token, _ = await create_session(redis_client, "usr_1", TTL)
    await redis_client.delete(f"session:{token}")

    assert await read_session(redis_client, token, TTL) is None


# --------------------------------------------------------------- deletion ---


async def test_delete_session_ends_it(redis_client: Redis) -> None:
    token, _ = await create_session(redis_client, "usr_1", TTL)

    await delete_session(redis_client, token)

    assert await read_session(redis_client, token, TTL) is None


async def test_delete_session_removes_it_from_the_user_index(redis_client: Redis) -> None:
    """A stale index entry would make revoke-all report the wrong count."""
    token, _ = await create_session(redis_client, "usr_1", TTL)

    await delete_session(redis_client, token)

    assert await smembers(redis_client, "user_sessions:usr_1") == set()


async def test_delete_session_tolerates_an_unknown_token(redis_client: Redis) -> None:
    await delete_session(redis_client, "never-existed")
    await delete_session(redis_client, "")


async def test_delete_user_sessions_ends_all_of_them(redis_client: Redis) -> None:
    """The reason the user index exists.

    A password change has to invalidate sessions the current request knows
    nothing about - other browsers, other devices.
    """
    tokens = [(await create_session(redis_client, "usr_1", TTL))[0] for _ in range(3)]

    ended = await delete_user_sessions(redis_client, "usr_1")

    assert ended == 3
    for token in tokens:
        assert await read_session(redis_client, token, TTL) is None


async def test_delete_user_sessions_leaves_other_users_alone(redis_client: Redis) -> None:
    mine, _ = await create_session(redis_client, "usr_1", TTL)
    theirs, _ = await create_session(redis_client, "usr_2", TTL)

    await delete_user_sessions(redis_client, "usr_1")

    assert await read_session(redis_client, mine, TTL) is None
    assert await read_session(redis_client, theirs, TTL) is not None


async def test_delete_user_sessions_with_none_live(redis_client: Redis) -> None:
    assert await delete_user_sessions(redis_client, "usr_nobody") == 0


# ------------------------------------------------------------------- csrf ---


def test_matching_csrf_tokens_pass() -> None:
    token = generate_csrf_token()

    assert tokens_match(token, token) is True


def test_differing_csrf_tokens_fail() -> None:
    assert tokens_match(generate_csrf_token(), generate_csrf_token()) is False


def test_missing_csrf_token_fails() -> None:
    """A form that omits the field must be rejected, not treated as a match."""
    token = generate_csrf_token()

    assert tokens_match(None, token) is False
    assert tokens_match("", token) is False
    assert tokens_match(token, None) is False
    assert tokens_match(None, None) is False
