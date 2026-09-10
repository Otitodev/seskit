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

#: How long a request-path AWS call may spend reading. Short on purpose: these
#: sit behind a page render, and an unreachable endpoint should fail fast enough
#: to show an error rather than hanging the dashboard.
REQUEST_READ_TIMEOUT = 15

#: Added to a long poll's wait to get its read timeout. Enough for the round
#: trip and for SQS answering a little after its own deadline, without being so
#: generous that a genuinely stuck connection sits there unnoticed.
POLL_TIMEOUT_MARGIN = 10

#: Keep AWS calls from becoming an unbounded wait on a request path. Three
#: attempts in adaptive mode covers a throttle; the connect timeout is short
#: because an unreachable endpoint should fail fast enough to render an error.
BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=REQUEST_READ_TIMEOUT,
)


def polling_config(wait_seconds: int) -> Config:
    """A config for a call that is *supposed* to wait.

    `BOTO_CONFIG` is written for the request path, where a short read timeout
    is the point - an unreachable endpoint should fail fast enough to render an
    error. SQS long polling is the opposite: the connection is held open until
    a message arrives or the wait expires, and on an idle queue that is the
    whole twenty seconds, every time.

    Sharing the request-path timeout made that a guaranteed failure. A 20
    second long poll against a 15 second read timeout times out on every empty
    receive, retries twice because the config asks for three attempts, and logs
    a stack trace a minute for ever - which is exactly what a deployment did.
    Delivery events never arrived, and nothing said why in terms anyone could
    act on.

    Derived from the wait rather than set beside it, so the two cannot drift
    apart again. The margin covers the round trip and SQS returning slightly
    after its own deadline.
    """
    return Config(
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=5,
        read_timeout=wait_seconds + POLL_TIMEOUT_MARGIN,
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
