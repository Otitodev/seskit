"""Constructing boto3 clients, and getting them off the event loop.

Two jobs, both easy to get subtly wrong.

**Blocking.** boto3 is synchronous. Called directly from a coroutine it holds
the event loop for the whole round trip to AWS - which on a bad day is seconds,
during which this process serves nobody. Every call therefore goes through
:func:`call`, which hands the work to a thread. ``aioboto3`` would avoid the
thread, but it is a second HTTP stack to keep current for no gain at the call
volume this phase produces.

**Credentials.** Resolved by botocore's own chain - instance role,
environment, shared file, container role, SSO - so this module resolves
nothing itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config

#: Keep AWS calls from becoming an unbounded wait on a request path. Three
#: attempts in adaptive mode covers a throttle; the connect timeout is short
#: because an unreachable endpoint should fail fast enough to render an error.
BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=15,
)


async def call[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one blocking boto3 call in a worker thread."""
    return await asyncio.to_thread(func, *args, **kwargs)


def build_session(region: str) -> boto3.Session:
    """A session for one region.

    No credential arguments, deliberately. Passing keys here is what §9 rules
    out, and leaving them out is what makes an instance role work untouched.
    """
    return boto3.Session(region_name=region)
