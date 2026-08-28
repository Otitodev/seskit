"""Project-level API rate limiting (§20)."""

from __future__ import annotations

import time

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from seskit_core.security.ratelimit import check_rate_limit, reset_rate_limit

LIMIT = 5
WINDOW = 60
PROJECT = "proj_01TEST"


async def _hit(redis: Redis, project: str = PROJECT, limit: int = LIMIT):  # type: ignore[no-untyped-def]
    return await check_rate_limit(redis, project, limit=limit, window_seconds=WINDOW)


async def test_the_first_request_is_allowed(redis_client: Redis) -> None:
    status = await _hit(redis_client)

    assert status.allowed is True
    assert status.limit == LIMIT
    assert status.remaining == LIMIT - 1


async def test_requests_up_to_the_limit_are_allowed(redis_client: Redis) -> None:
    for _ in range(LIMIT):
        assert (await _hit(redis_client)).allowed is True


async def test_the_request_past_the_limit_is_refused(redis_client: Redis) -> None:
    for _ in range(LIMIT):
        await _hit(redis_client)

    assert (await _hit(redis_client)).allowed is False


async def test_remaining_never_goes_negative(redis_client: Redis) -> None:
    """It is rendered into a header, and a negative budget is nonsense."""
    for _ in range(LIMIT + 4):
        await _hit(redis_client)
    status = await _hit(redis_client)

    assert status.remaining == 0


async def test_staying_over_the_limit_keeps_refusing(redis_client: Redis) -> None:
    """Counting happens before the comparison, so continued hammering does not
    slip through on a race between two concurrent reads.
    """
    for _ in range(LIMIT + 1):
        await _hit(redis_client)

    for _ in range(5):
        assert (await _hit(redis_client)).allowed is False


async def test_projects_do_not_share_an_allowance(redis_client: Redis) -> None:
    for _ in range(LIMIT + 1):
        await _hit(redis_client, "proj_01BUSY")

    assert (await _hit(redis_client, "proj_01QUIET")).allowed is True


async def test_the_window_resets(redis_client: Redis) -> None:
    """Simulated by clearing the counter rather than by waiting a minute."""
    for _ in range(LIMIT + 1):
        await _hit(redis_client)
    assert (await _hit(redis_client)).allowed is False

    await reset_rate_limit(redis_client, PROJECT, window_seconds=WINDOW)

    assert (await _hit(redis_client)).allowed is True


async def test_the_counter_expires_so_it_cannot_leak(redis_client: Redis) -> None:
    """Without the TTL, every project that ever called the API would leave a
    counter in Redis for ever.
    """
    await _hit(redis_client)

    keys = await redis_client.keys("ratelimit:*")
    assert keys
    assert await redis_client.ttl(keys[0]) > 0


async def test_the_expiry_is_not_pushed_forward_by_later_requests(
    redis_client: Redis,
) -> None:
    """``EXPIRE ... NX`` sets the TTL only when the counter is created.

    Refreshing it per request would keep moving the window's end away, and a
    project under sustained load would never get its allowance back.
    """
    await _hit(redis_client)
    key = (await redis_client.keys("ratelimit:*"))[0]
    await redis_client.expire(key, 5)

    await _hit(redis_client)

    assert await redis_client.ttl(key) <= 5


async def test_reset_at_is_the_end_of_the_current_window(redis_client: Redis) -> None:
    status = await _hit(redis_client)

    now = int(time.time())
    assert now < status.reset_at <= now + WINDOW


async def test_retry_after_is_never_zero(redis_client: Redis) -> None:
    """A Retry-After of 0 invites an immediate retry that is certain to fail."""
    status = await _hit(redis_client)

    assert status.retry_after >= 1


# ------------------------------------------------------------ degradation ---


class BrokenRedis:
    """A Redis whose every pipeline execution fails."""

    def pipeline(self) -> BrokenRedis:
        return self

    def incr(self, *args: object, **kwargs: object) -> None:
        return None

    def expire(self, *args: object, **kwargs: object) -> None:
        return None

    async def execute(self) -> object:
        raise RedisConnectionError("redis is down")


async def test_an_unreachable_redis_allows_the_request() -> None:
    """A limiter is a guard on a working service, not a dependency of it.

    Refusing every send because the counter is unavailable turns a Redis blip
    into a full API outage - a much worse failure than briefly not enforcing a
    quota.
    """
    status = await check_rate_limit(
        BrokenRedis(),  # type: ignore[arg-type]
        PROJECT,
        limit=LIMIT,
        window_seconds=WINDOW,
    )

    assert status.allowed is True


async def test_failing_open_still_reports_a_usable_budget() -> None:
    """The headers are rendered from this either way, so the numbers have to be
    coherent rather than zero or negative.
    """
    status = await check_rate_limit(
        BrokenRedis(),  # type: ignore[arg-type]
        PROJECT,
        limit=LIMIT,
        window_seconds=WINDOW,
    )

    assert status.limit == LIMIT
    assert status.remaining == LIMIT
    assert status.retry_after >= 1
