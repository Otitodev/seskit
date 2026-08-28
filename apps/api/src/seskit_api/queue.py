"""Enqueuing work for the worker.

A separate ARQ connection rather than reusing the application's Redis client:
ARQ has its own serialisation and key layout, and sharing a client would mean
two libraries with different assumptions writing through the same connection.

Held on app state and opened at startup, because building a pool per request
would spend a connection handshake on every send.
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request
from seskit_core.config import Settings


async def create_queue(settings: Settings) -> ArqRedis:
    """Open the pool the API enqueues through."""
    return await create_pool(RedisSettings.from_dsn(str(settings.REDIS_URL)))


def get_queue(request: Request) -> ArqRedis:
    """The pool from app state.

    A dependency rather than a module global so a test can override it - and so
    the failure mode when it is missing is an obvious one at startup rather than
    a confusing one at send time.
    """
    queue: ArqRedis = request.app.state.queue
    return queue
