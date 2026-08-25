"""Async Redis client.

Shared by the API (rate limiting, Phase 20) and the worker (ARQ's queue backend).
"""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from seskit_core.config import Settings, get_settings

_client: Redis | None = None


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
