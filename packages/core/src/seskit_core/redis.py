"""Async Redis client.

Shared by the API (rate limiting, Phase 20) and the worker (ARQ's queue backend).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis, from_url

from seskit_core.config import Settings, get_settings

_client: Redis | None = None


# redis-py annotates these as ``Awaitable[T] | T`` because one class serves both
# the sync and async clients. On the async client the awaitable branch is the
# only one that happens, but mypy cannot know that, so every call site would
# otherwise need its own cast. These wrappers hold the cast in one place.


async def hgetall(client: Redis, key: str) -> dict[str, str]:
    return await cast("Awaitable[dict[str, str]]", client.hgetall(key))


async def smembers(client: Redis, key: str) -> set[str]:
    return await cast("Awaitable[set[str]]", client.smembers(key))


def create_client(settings: Settings | None = None) -> Redis:
    """Build a new Redis client. Prefer :func:`get_redis` in application code."""
    settings = settings or get_settings()
    client: Redis = from_url(  # type: ignore[no-untyped-call]
        str(settings.REDIS_URL),
        encoding="utf-8",
        decode_responses=True,
        health_check_interval=30,
    )
    return client


def get_redis() -> Redis:
    """Return the process-wide Redis client, creating it on first use."""
    global _client
    if _client is None:
        _client = create_client()
    return _client


async def check_redis(client: Redis | None = None) -> bool:
    """Return True if Redis answers a PING. Backs the readiness probe."""
    client = client or get_redis()
    return bool(await client.ping())


async def close_redis() -> None:
    """Close the client. Called on application shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
