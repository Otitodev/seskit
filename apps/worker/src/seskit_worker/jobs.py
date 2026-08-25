"""Background jobs.

Phase 1 defines only ``ping`` - enough to prove enqueue -> execute works before
anything depends on it. Real jobs (SES sends, webhook deliveries) arrive in
Phases 6 and 8.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from seskit_core.logging import get_logger

logger = get_logger(__name__)


async def ping(ctx: dict[str, Any], message: str = "pong") -> str:
    """Log and echo a message.

    A no-op by design. Its value is as a health check for the queue itself:
    if this round-trips, Redis, the worker process, and job serialisation are
    all working.
    """
    logger.info("job_ping", job_id=ctx.get("job_id"), message=message)
    return f"{message} @ {datetime.now(UTC).isoformat()}"
