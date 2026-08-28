"""API key issuance, verification, and revocation.

The verification path runs on every request a customer application makes, so it
is written to cost one Redis round trip in the common case rather than a
database query.

Redis is a cache here, never the source of truth, and every use of it below
degrades rather than fails. The database can answer every question Redis is
asked; losing the cache should cost latency, not availability.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.logging import get_logger
from seskit_core.models import APIKey, utcnow
from seskit_core.security.api_keys import display_prefix, generate_key, hash_key

logger = get_logger(__name__)

#: Resolved key hash -> project id. Short-lived; revocation deletes the entry
#: rather than waiting for it to expire.
CACHE_KEY_PREFIX = "apikey:"

#: Marks that ``last_used_at`` was written recently for a key.
LAST_USED_KEY_PREFIX = "apikey_used:"


@dataclass(frozen=True)
class IssuedKey:
    """A newly created key, and the only time the raw value exists.

    Returned from :func:`create_api_key` and passed straight to the template
    that shows it once. Never stored, never logged.
    """

    api_key: APIKey
    raw_key: str


def _cache_key(hashed: str) -> str:
    return f"{CACHE_KEY_PREFIX}{hashed}"


def _last_used_key(key_id: str) -> str:
    return f"{LAST_USED_KEY_PREFIX}{key_id}"


async def create_api_key(session: AsyncSession, *, project_id: str, name: str) -> IssuedKey:
    """Mint a key for a project.

    The caller owns the transaction, as elsewhere in this layer, so the key and
    whatever else the request is doing commit together.
    """
    raw = generate_key()

    api_key = APIKey(
        project_id=project_id,
        name=name,
        key_prefix=display_prefix(raw),
        hashed_key=hash_key(raw),
    )
    session.add(api_key)
    await session.flush()

    return IssuedKey(api_key=api_key, raw_key=raw)


async def list_api_keys(session: AsyncSession, project_id: str) -> list[APIKey]:
    """Every key for a project, newest first.

    ULIDs sort by creation time, so descending id is reverse-chronological and
    needs no extra column. Revoked keys stay in the list - a key that vanishes
    on revocation leaves no record that it ever existed.
    """
    result = await session.scalars(
        select(APIKey).where(APIKey.project_id == project_id).order_by(APIKey.id.desc())
    )
    return list(result)


async def get_owned_api_key(
    session: AsyncSession, *, key_id: str, project_id: str
) -> APIKey | None:
    """Return the key only if it belongs to this project.

    Ownership is part of the query rather than a check afterwards, matching
    ``get_owned_project``: there is no path that loads the row and then forgets
    to compare.
    """
    api_key: APIKey | None = await session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.project_id == project_id)
    )
    return api_key


async def verify_api_key(
    session: AsyncSession, redis: Redis, *, raw_key: str, cache_ttl_seconds: int
) -> str | None:
    """Return the project id this key authenticates for, or None.

    Checks Redis first: a busy sender would otherwise hit Postgres on every
    call for a value that almost never changes.

    One return value for every failure - unknown, malformed, revoked - so a
    caller cannot tell them apart, and the route returns one generic message.
    """
    hashed = hash_key(raw_key)

    try:
        cached = await redis.get(_cache_key(hashed))
    except RedisError:
        # Fall through to the database. Refusing a valid key because the cache
        # is unreachable would turn a Redis outage into an authentication
        # outage, and the row below is the authoritative answer anyway.
        logger.warning("api_key_cache_unavailable", exc_info=True)
        cached = None

    if cached is not None:
        return str(cached)

    api_key: APIKey | None = await session.scalar(
        select(APIKey).where(APIKey.hashed_key == hashed, APIKey.revoked_at.is_(None))
    )
    if api_key is None:
        # Deliberately not cached. Caching misses would let anyone fill Redis
        # with entries by presenting made-up keys.
        return None

    try:
        await redis.set(_cache_key(hashed), api_key.project_id, ex=cache_ttl_seconds)
    except RedisError:
        # The key is valid; not being able to remember that is not a reason to
        # reject it. The next request simply pays for the query again.
        logger.warning("api_key_cache_write_failed", key_id=api_key.id, exc_info=True)

    return api_key.project_id


async def touch_last_used(
    session: AsyncSession, redis: Redis, *, raw_key: str, interval_seconds: int
) -> None:
    """Record that a key was used, at most once per interval.

    Writing on every request would add a database write to every API call, for
    a column read at most a few times a day. The Redis marker holds the write
    down to once per interval, which keeps the column accurate to the minute -
    all the UI claims when it says "last used 2 hours ago".
    """
    hashed = hash_key(raw_key)

    api_key: APIKey | None = await session.scalar(select(APIKey).where(APIKey.hashed_key == hashed))
    if api_key is None:
        return

    # SET NX succeeds only when no marker exists, so exactly one caller in the
    # interval performs the write even under concurrent requests.
    try:
        first_in_interval = await redis.set(
            _last_used_key(api_key.id), "1", ex=interval_seconds, nx=True
        )
    except RedisError:
        # Skip the write rather than fail the request. This column is a
        # convenience for the dashboard; nothing depends on it being current.
        logger.warning("api_key_last_used_skipped", key_id=api_key.id, exc_info=True)
        return

    if not first_in_interval:
        return

    api_key.last_used_at = utcnow()
    await session.flush()


async def revoke_api_key(session: AsyncSession, redis: Redis, api_key: APIKey) -> None:
    """Revoke a key and stop it authenticating immediately.

    The cache entry is deleted rather than left to expire. Waiting for the TTL
    would leave a revoked key working for up to a minute - exactly the window
    that matters when a key is being revoked *because* it leaked.

    Revocation is permanent. The raw value may be in a log or a repository
    somewhere, so a revoked key is never reactivated.
    """
    if api_key.revoked_at is None:
        api_key.revoked_at = utcnow()
        await session.flush()

    try:
        await redis.delete(_cache_key(api_key.hashed_key))
    except RedisError:
        # The revocation is already committed to the database, which is what
        # makes it real. Losing the cache eviction means the key may keep
        # authenticating until the entry expires - which is precisely the
        # backstop the short TTL exists to provide. Raising here would instead
        # abandon the revocation altogether, which is far worse.
        logger.warning("api_key_cache_evict_failed", key_id=api_key.id, exc_info=True)
