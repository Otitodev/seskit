"""Connecting a project to an AWS account (§8).

What "connecting" means here is narrow and worth stating: SESKit asks AWS who
the configured identity is and what it may do, then records the answer. It
creates nothing in AWS. Disconnecting deletes a local row and leaves the AWS
account untouched, because there is nothing there to undo.

**On caching.** The plan for this phase called for a Redis cache so that
rendering the page would not call AWS. Persisting the answer to Postgres already
achieves that - the page reads the row - so a second copy in Redis would be a
cache of a cache. What Redis is used for instead is the thing the row cannot do:
gating how often a live check may run. Without it, holding down Refresh sends a
request to AWS every time, and AWS answers by throttling the account. Same
setting, same TTL, doing something that is actually load-bearing - the same
pattern as ``touch_last_used`` in the API key service.
"""

from __future__ import annotations

from collections.abc import Callable

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.errors import APIError
from seskit_core.logging import get_logger
from seskit_core.models import AWSConnection, ConnectionStatus, utcnow
from seskit_core.providers import AccountStatus, EmailProvider

logger = get_logger(__name__)

#: Marks that a live AWS check ran recently for a project.
CHECK_MARKER_PREFIX = "aws_checked:"

#: Builds a provider for a region. Injected so tests - and, later, a second
#: provider - can substitute one without the service importing an adapter.
ProviderFactory = Callable[[str], EmailProvider]


def _marker_key(project_id: str) -> str:
    return f"{CHECK_MARKER_PREFIX}{project_id}"


async def get_connection(session: AsyncSession, project_id: str) -> AWSConnection | None:
    """The project's connection, if it has one."""
    connection: AWSConnection | None = await session.scalar(
        select(AWSConnection).where(AWSConnection.project_id == project_id)
    )
    return connection


async def check_is_allowed(redis: Redis, project_id: str, *, interval_seconds: int) -> bool:
    """Whether a live AWS call may run for this project now.

    ``SET NX`` succeeds only when no marker exists, so exactly one caller per
    interval gets through even if several refreshes arrive at once. A user who
    holds down Refresh gets the stored answer rather than a throttled account.
    """
    if interval_seconds <= 0:
        # A configured interval of zero means "do not throttle". Passing it to
        # Redis would raise - EX must be positive - so a setting a user is
        # entitled to choose would break refresh entirely.
        return True

    allowed = await redis.set(_marker_key(project_id), "1", ex=interval_seconds, nx=True)
    return bool(allowed)


async def clear_check_marker(redis: Redis, project_id: str) -> None:
    """Let the next check run immediately.

    Used on connect and disconnect: both are deliberate acts by a user who is
    watching, and making them wait out a marker set by a previous project state
    would be nonsense.
    """
    await redis.delete(_marker_key(project_id))


async def connect_aws(
    session: AsyncSession,
    redis: Redis,
    provider_factory: ProviderFactory,
    *,
    project_id: str,
    region: str,
) -> AWSConnection:
    """Verify the AWS identity and record what it is.

    Raises the normalised ``APIError`` on failure rather than returning a
    half-built row, so the route can show the user what AWS actually said.
    """
    provider = provider_factory(region)
    connection = await get_connection(session, project_id)

    try:
        status = await provider.verify_account()
    except APIError as error:
        # An existing connection that has stopped working should look broken,
        # not stale. A project that never connected gets no row - there is
        # nothing to describe.
        if connection is not None:
            connection.status = ConnectionStatus.ERROR.value
            connection.last_error = error.message
            connection.last_checked_at = utcnow()
            await session.flush()
        logger.info(
            "aws_connect_failed",
            project_id=project_id,
            region=region,
            error_type=error.error_type.value,
        )
        raise

    connection = _apply(connection, status, project_id=project_id, region=region)
    session.add(connection)
    await session.flush()
    await clear_check_marker(redis, project_id)

    logger.info(
        "aws_connected",
        project_id=project_id,
        region=region,
        sandbox=status.sandbox,
        credential_mode=status.credential_mode.value,
    )
    return connection


async def refresh_connection(
    session: AsyncSession,
    redis: Redis,
    provider_factory: ProviderFactory,
    connection: AWSConnection,
    *,
    interval_seconds: int,
) -> AWSConnection:
    """Re-check an existing connection against AWS, if the interval allows.

    Returns the connection either way. A refusal is not an error: the stored
    answer is still the answer, and it carries ``last_checked_at`` so the page
    can say how old it is.
    """
    if not await check_is_allowed(redis, connection.project_id, interval_seconds=interval_seconds):
        logger.debug("aws_refresh_skipped", project_id=connection.project_id)
        return connection

    provider = provider_factory(connection.region)

    try:
        status = await provider.verify_account()
    except APIError as error:
        connection.status = ConnectionStatus.ERROR.value
        connection.last_error = error.message
        connection.last_checked_at = utcnow()
        await session.flush()
        raise

    _apply(connection, status, project_id=connection.project_id, region=connection.region)
    await session.flush()
    return connection


async def disconnect_aws(session: AsyncSession, redis: Redis, connection: AWSConnection) -> None:
    """Forget the connection.

    Local only. Nothing was created in AWS, so there is nothing there to remove
    - and deleting SES identities on a user's behalf is not something a
    "disconnect" button should be able to do.
    """
    project_id = connection.project_id
    await session.delete(connection)
    await session.flush()
    await clear_check_marker(redis, project_id)
    logger.info("aws_disconnected", project_id=project_id)


def _apply(
    connection: AWSConnection | None,
    status: AccountStatus,
    *,
    project_id: str,
    region: str,
) -> AWSConnection:
    """Write a provider's answer onto the row, creating it if needed."""
    if connection is None:
        connection = AWSConnection(project_id=project_id, region=region)

    connection.region = region
    connection.aws_account_id = status.account_id
    connection.credential_mode = status.credential_mode.value
    connection.status = ConnectionStatus.CONNECTED.value
    connection.sandbox = status.sandbox
    connection.sending_enabled = status.sending_enabled
    connection.enforcement_status = status.enforcement_status
    connection.max_24_hour_send = status.quota.max_24_hour_send
    connection.max_send_rate = status.quota.max_send_rate
    connection.sent_last_24_hours = status.quota.sent_last_24_hours
    connection.last_checked_at = utcnow()
    connection.last_error = None
    return connection
