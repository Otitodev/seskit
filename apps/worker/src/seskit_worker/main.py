"""ARQ worker entrypoint.

Run with:  uv run arq seskit_worker.main.WorkerSettings
"""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings
from seskit_core.config import get_settings
from seskit_core.db import dispose_engine
from seskit_core.logging import configure_logging, get_logger

from seskit_worker.identities import recheck_identities
from seskit_worker.jobs import ping

logger = get_logger(__name__)


def build_redis_settings() -> RedisSettings:
    """Translate the app's Redis URL into ARQ's connection settings."""
    return RedisSettings.from_dsn(str(get_settings().REDIS_URL))


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(log_level=settings.LOG_LEVEL, json_output=not settings.is_local)
    logger.info("worker_started", environment=settings.ENVIRONMENT.value)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    logger.info("worker_stopped")


class WorkerSettings:
    """ARQ worker configuration.

    ARQ reads these off the class ``__dict__`` rather than instantiating the
    class, so every entry must be a plain value - a ``staticmethod`` would be
    handed to the worker as the method object instead of its result. That is
    why ``redis_settings`` is evaluated here at import time.
    """

    functions = [ping, recheck_identities]  # noqa: RUF012 - ARQ needs a plain attribute

    #: Hourly, on the hour. The hour is not the interval - each identity has its
    #: own due check, so most passes find nothing to do. That is the intended
    #: shape: the tick is cheap, the SES calls are not.
    cron_jobs = [  # noqa: RUF012 - as above
        # Its own timeout: job_timeout below suits a single unit of work,
        # but this one walks every due identity and each walk is a round
        # trip to SES. Inheriting 60s would kill a legitimate pass on an
        # instance with a lot of domains.
        cron(recheck_identities, minute=0, run_at_startup=False, timeout=300)
    ]

    on_startup = startup
    on_shutdown = shutdown
    redis_settings = build_redis_settings()
    max_tries = 3
    job_timeout = 60
