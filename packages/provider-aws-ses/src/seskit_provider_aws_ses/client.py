"""Constructing boto3 clients, and getting them off the event loop.

Two jobs, both easy to get subtly wrong.

**Blocking.** boto3 is synchronous. Called directly from a coroutine it holds
the event loop for the whole round trip to AWS - which on a bad day is seconds,
during which this process serves nobody. Every call therefore goes through
:func:`call`, which hands the work to a thread. ``aioboto3`` would avoid the
thread, but it is a second HTTP stack to keep current for no gain at the call
volume this phase produces.

**Credentials.** §9: resolved the standard boto3 way and never stored. botocore
already implements that chain - instance role, environment, shared file,
container role, SSO - so this module resolves nothing itself. It only asks
botocore which link of the chain answered, so the connection row can record it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from seskit_core.providers.types import CredentialMode

#: Keep AWS calls from becoming an unbounded wait on a request path. Three
#: attempts in adaptive mode covers a throttle; the connect timeout is short
#: because an unreachable endpoint should fail fast enough to render an error.
BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=15,
)

#: botocore names its credential providers in ``Credentials.method``. Mapping
#: them rather than passing the raw string through keeps a botocore rename from
#: silently changing what a stored row means.
_CREDENTIAL_METHODS: dict[str, CredentialMode] = {
    "env": CredentialMode.ENVIRONMENT,
    "shared-credentials-file": CredentialMode.SHARED_CREDENTIALS_FILE,
    "config-file": CredentialMode.CONFIG_FILE,
    "iam-role": CredentialMode.IAM_ROLE,
    "ec2-instance-metadata": CredentialMode.IAM_ROLE,
    "container-role": CredentialMode.CONTAINER_ROLE,
    "assume-role": CredentialMode.ASSUME_ROLE,
    "assume-role-with-web-identity": CredentialMode.CONTAINER_ROLE,
    "sso": CredentialMode.SSO,
}


async def call[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one blocking boto3 call in a worker thread."""
    return await asyncio.to_thread(func, *args, **kwargs)


def build_session(region: str) -> boto3.Session:
    """A session for one region.

    No credential arguments, deliberately. Passing keys here is what §9 rules
    out, and leaving them out is what makes an instance role work untouched.
    """
    return boto3.Session(region_name=region)


def resolve_credential_mode(session: boto3.Session) -> CredentialMode:
    """Which link of the credential chain answered.

    Recorded because "why did this stop working" has a very different answer for
    an expired environment variable than for a detached instance role. Returns
    ``UNKNOWN`` rather than raising when nothing resolves - the caller finds out
    for real on the first API call, which produces a far better error than a
    guess made here.
    """
    try:
        credentials = session.get_credentials()
    except Exception:
        return CredentialMode.UNKNOWN

    if credentials is None:
        return CredentialMode.UNKNOWN

    method = getattr(credentials, "method", "") or ""
    return _CREDENTIAL_METHODS.get(method, CredentialMode.UNKNOWN)
