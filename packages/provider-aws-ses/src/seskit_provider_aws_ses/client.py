"""Constructing boto3 clients, and getting them off the event loop.

Two jobs, both easy to get subtly wrong.

**Blocking.** boto3 is synchronous. Called directly from a coroutine it holds
the event loop for the whole round trip to AWS - which on a bad day is seconds,
during which this process serves nobody. Every call therefore goes through
:func:`call`, which hands the work to a thread. ``aioboto3`` would avoid the
thread, but it is a second HTTP stack to keep current for no gain at the call
volume this phase produces.

**Credentials.** Passed in, never resolved. SESKit stores an access key per
project and hands it here, so nothing depends on the environment the process
happens to run in - which is what lets two projects on one instance reach two
different AWS accounts, something botocore's chain could not express.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from seskit_core.providers.types import AWSCredentials

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


def build_session(region: str, credentials: AWSCredentials) -> boto3.Session:
    """A session for one region, on one project's credentials.

    Explicit rather than ambient. botocore would happily fall back to an
    instance role or an environment variable if these were omitted, and that
    fallback is the failure worth designing out: a project whose stored key was
    wrong would quietly send as whatever the *host* could reach, which is a
    silent cross-account send rather than an error.
    """
    return boto3.Session(
        region_name=region,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
    )
