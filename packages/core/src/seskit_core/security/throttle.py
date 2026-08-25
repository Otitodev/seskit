"""Failed-login throttling.

The login form is the highest-value target on the dashboard and the only place
an unauthenticated caller can guess a secret, so it gets its own limiter. This
is separate from §20's project-level API rate limiting, which lands in its own
phase and answers a different question.

Counted per email **and** client address: keying on the address alone lets one
attacker behind a shared NAT lock out an office, and keying on the email alone
lets anyone lock a known user out of their own account by failing on purpose.
Requiring both to match keeps a real user's own attempts unaffected by someone
else's.
"""

from __future__ import annotations

from redis.asyncio import Redis

THROTTLE_KEY_PREFIX = "login_attempts:"


def _key(email: str, client: str) -> str:
    return f"{THROTTLE_KEY_PREFIX}{email}:{client}"


async def record_failure(redis: Redis, email: str, client: str, window_seconds: int) -> int:
    """Count one failed attempt and return the running total.

    The window is fixed rather than sliding: the first failure starts the clock
    and the counter disappears when it runs out. Cruder than a sliding window,
    but it is one round trip and needs no stored history.
    """
    key = _key(email, client)
    pipeline = redis.pipeline()
    pipeline.incr(key)
    pipeline.expire(key, window_seconds, nx=True)
    results = await pipeline.execute()
    return int(results[0])


async def is_throttled(redis: Redis, email: str, client: str, max_attempts: int) -> bool:
    """Whether this email and address have failed too often to keep trying."""
    raw = await redis.get(_key(email, client))
    return raw is not None and int(raw) >= max_attempts


async def clear(redis: Redis, email: str, client: str) -> None:
    """Reset the counter after a successful login.

    Without this, a user who mistypes a few times and then gets in would still
    be locked out on their next visit.
    """
    await redis.delete(_key(email, client))
