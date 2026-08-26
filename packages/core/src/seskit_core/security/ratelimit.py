"""Project-level API rate limiting (§20).

Separate from ``throttle.py``, which guards the login form and answers a
different question. This one caps how fast a project may call the public API.

**Fixed window**, not a sliding log: two Redis commands and O(1) memory per
project, against a sliding window's per-request bookkeeping. The known cost is
a boundary burst - a caller that spends its whole allowance at the end of one
window and again at the start of the next gets up to 2x the limit across the
seam. At 100/minute that is 200 requests in a pathological two-second span,
which is not worth a more complex limiter. Recorded here rather than
rediscovered later.

Counted **per project, not per key**, so minting a second key cannot be used to
buy more quota.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

RATE_LIMIT_KEY_PREFIX = "ratelimit:"


@dataclass(frozen=True)
class RateLimitStatus:
    """The outcome of one rate-limit check.

    Carries enough for the caller to build the ``X-RateLimit-*`` headers. A
    client that cannot see its budget can only discover the limit by hitting
    it.
    """

    allowed: bool
    limit: int
    remaining: int
    #: Unix seconds at which the current window ends.
    reset_at: int

    @property
    def retry_after(self) -> int:
        """Seconds until the window resets, floored at 1.

        Zero would invite an immediate retry that is certain to fail.
        """
        return max(1, self.reset_at - int(time.time()))


def _window_start(now: float, window_seconds: int) -> int:
    return int(now) // window_seconds * window_seconds


def _key(project_id: str, window_start: int) -> str:
    return f"{RATE_LIMIT_KEY_PREFIX}{project_id}:{window_start}"


async def check_rate_limit(
    redis: Redis, project_id: str, *, limit: int, window_seconds: int
) -> RateLimitStatus:
    """Count this request against the project's allowance.

    Counts first and compares afterwards, so a caller that keeps hammering
    while limited stays limited rather than slipping through on a race between
    two concurrent reads.
    """
    now = time.time()
    start = _window_start(now, window_seconds)
    reset_at = start + window_seconds

    pipeline = redis.pipeline()
    pipeline.incr(_key(project_id, start))
    # Only on creation: refreshing the TTL on every request would keep pushing
    # the window's end away and the counter would never reset.
    pipeline.expire(_key(project_id, start), window_seconds, nx=True)
    results = await pipeline.execute()

    used = int(results[0])

    return RateLimitStatus(
        allowed=used <= limit,
        limit=limit,
        remaining=max(0, limit - used),
        reset_at=reset_at,
    )


async def reset_rate_limit(redis: Redis, project_id: str, *, window_seconds: int) -> None:
    """Clear a project's current window. For tests and for support."""
    await redis.delete(_key(project_id, _window_start(time.time(), window_seconds)))
