"""Dashboard sessions, stored in Redis.

The cookie carries an opaque random token; everything about the session lives
server-side. That is what makes a session revocable - a signed cookie stays
valid until it expires no matter what the server thinks, so "log out
everywhere" and "invalidate on password change" are impossible with one.

Layout in Redis:

    session:{token}          hash  - user_id, csrf_token, created_at
    user_sessions:{user_id}  set   - every live token for that user
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from redis.asyncio import Redis

from seskit_core.models.base import utcnow
from seskit_core.redis import hgetall, smembers

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"

#: 256 bits. Long enough that guessing is not a threat model.
TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class SessionData:
    user_id: str
    csrf_token: str
    created_at: str

    #: Which project the dashboard is currently showing. Lives in the session
    #: rather than the URL so a switch persists across pages, and is always
    #: re-checked against ownership on read - a session is not a capability.
    current_project_id: str | None = None


def _session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{token}"


def _user_sessions_key(user_id: str) -> str:
    return f"{USER_SESSIONS_KEY_PREFIX}{user_id}"


def generate_token() -> str:
    """Return a new opaque session token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


async def create_session(redis: Redis, user_id: str, ttl_seconds: int) -> tuple[str, SessionData]:
    """Start a session and return its token.

    Callers must use this at login rather than reusing whatever token the
    browser already had: keeping a pre-login token is the session-fixation bug,
    where an attacker plants a known token and inherits the session once the
    victim signs in.
    """
    token = generate_token()
    data = SessionData(
        user_id=user_id,
        csrf_token=secrets.token_urlsafe(TOKEN_BYTES),
        created_at=utcnow().isoformat(),
    )

    pipeline = redis.pipeline()
    pipeline.hset(
        _session_key(token),
        mapping={
            "user_id": data.user_id,
            "csrf_token": data.csrf_token,
            "created_at": data.created_at,
        },
    )
    pipeline.expire(_session_key(token), ttl_seconds)
    pipeline.sadd(_user_sessions_key(user_id), token)
    # The index outlives any single session so a stale entry cannot pin it
    # forever; entries for expired sessions are pruned on read.
    pipeline.expire(_user_sessions_key(user_id), ttl_seconds)
    await pipeline.execute()

    return token, data


async def read_session(redis: Redis, token: str, ttl_seconds: int) -> SessionData | None:
    """Return the session, refreshing its expiry, or None if it is gone.

    The TTL is extended on every read, so the timeout is idle rather than
    absolute - an active user is not signed out mid-task.
    """
    if not token:
        return None

    raw = await hgetall(redis, _session_key(token))
    if not raw:
        return None

    await redis.expire(_session_key(token), ttl_seconds)

    return SessionData(
        user_id=raw["user_id"],
        csrf_token=raw["csrf_token"],
        created_at=raw["created_at"],
        current_project_id=raw.get("current_project_id") or None,
    )


async def set_current_project(redis: Redis, token: str, project_id: str, ttl_seconds: int) -> None:
    """Remember which project the dashboard is showing.

    Callers must confirm the user owns the project first. Storing it here is a
    convenience, never an authorisation decision - every read re-checks
    ownership, so a tampered session grants nothing.
    """
    key = _session_key(token)
    pipeline = redis.pipeline()
    pipeline.hset(key, "current_project_id", project_id)
    pipeline.expire(key, ttl_seconds)
    await pipeline.execute()


async def delete_session(redis: Redis, token: str) -> None:
    """End one session. Used by logout."""
    if not token:
        return

    raw = await hgetall(redis, _session_key(token))
    pipeline = redis.pipeline()
    pipeline.delete(_session_key(token))
    if raw:
        pipeline.srem(_user_sessions_key(raw["user_id"]), token)
    await pipeline.execute()


async def delete_user_sessions(redis: Redis, user_id: str) -> int:
    """End every session for a user, and return how many were live.

    The reason the user index exists: a password change or a compromised account
    has to invalidate sessions the current request knows nothing about.
    """
    index_key = _user_sessions_key(user_id)
    tokens = await smembers(redis, index_key)
    if not tokens:
        return 0

    pipeline = redis.pipeline()
    for token in tokens:
        pipeline.delete(_session_key(token))
    pipeline.delete(index_key)
    results = await pipeline.execute()

    # Only count sessions that were actually still alive; the index can hold
    # tokens whose sessions already expired.
    return sum(1 for deleted in results[:-1] if deleted)
