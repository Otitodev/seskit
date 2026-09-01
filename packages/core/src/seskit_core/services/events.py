"""Setting up and removing event infrastructure (§15).

The service layer's share of the work: decide *whether* to create or remove
anything, and record what happened. The creating and removing itself is the
provider's, behind :class:`~seskit_core.providers.EventProvisioner`, so core
still imports no adapter (§32.8).

**The trap this exists for.** §9 puts one AWS identity behind the whole
instance, so two projects in the same region share an account - and if they
share a region they share the queue, the topic and the configuration set,
because those are named per region, not per project. Tearing down when the
second project disconnects would take the first project's event pipeline with
it, and nothing would report an error: SES would simply stop publishing, and
deliveries would go quiet.

So teardown counts other users of the same infrastructure first and removes
nothing while one remains. This is the same shape as the refcount in
``services.identities``, and for the same reason - what looks like this
project's resource is shared.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.logging import get_logger
from seskit_core.models import AWSConnection
from seskit_core.providers import EventProvisioner

logger = get_logger(__name__)

#: Builds a provisioner for a region. Injected for the same reason as
#: ``ProviderFactory``: so this module never imports an adapter.
ProvisionerFactory = Callable[[str], EventProvisioner]


def queue_name_for(prefix: str) -> str:
    """The SQS queue SESKit polls.

    One per instance rather than per project. Events carry the SES message id
    and are correlated by it, so a second queue would only mean a second poller
    reading the same notifications.
    """
    return f"{prefix}-events"


def topic_name_for(prefix: str) -> str:
    return f"{prefix}-events"


async def count_other_users(session: AsyncSession, connection: AWSConnection) -> int:
    """How many *other* projects rely on this same infrastructure.

    Matched on the region and the account, because that is what determines
    whether two connections point at the same physical queue and topic - not on
    the project, and not on the stored ARN, which a row that failed halfway
    through provisioning may not have.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(AWSConnection)
        .where(
            AWSConnection.id != connection.id,
            AWSConnection.region == connection.region,
            AWSConnection.aws_account_id == connection.aws_account_id,
            AWSConnection.configuration_set.is_not(None),
        )
    )
    return int(total or 0)


async def distinct_event_queues(session: AsyncSession) -> list[tuple[str, str]]:
    """Every queue that needs polling, as ``(region, queue_url)`` pairs.

    Distinct, because projects sharing a region share a queue - the same
    reasoning the teardown refcount rests on. Polling once per project would
    mean several consumers racing for the same messages, each one stealing
    events from the others' batches and doing the same work twice.

    Correlation is by SES message id, so a single consumer serves every project
    on that queue without needing to know whose message it is holding.
    """
    rows = await session.execute(
        select(AWSConnection.region, AWSConnection.event_queue_url)
        .where(AWSConnection.event_queue_url.is_not(None))
        .distinct()
    )
    return [(region, url) for region, url in rows if url]


async def setup_events(
    session: AsyncSession,
    provisioner_factory: ProvisionerFactory,
    connection: AWSConnection,
    *,
    resource_prefix: str,
    configuration_set: str,
    https_endpoint: str | None = None,
) -> AWSConnection:
    """Create the AWS resources and record what was created.

    Idempotent at both layers: the provisioner converges on the same resources,
    and this rewrites the same columns. Running it again is how a user repairs
    infrastructure they deleted by hand in the console.
    """
    provisioner = provisioner_factory(connection.region)

    infrastructure = await provisioner.provision_events(
        queue_name=queue_name_for(resource_prefix),
        topic_name=topic_name_for(resource_prefix),
        configuration_set=configuration_set,
        https_endpoint=https_endpoint,
        track_opens_and_clicks=connection.track_opens_and_clicks,
    )

    connection.record_event_infrastructure(infrastructure)
    await session.flush()

    logger.info(
        "events_set_up",
        project_id=connection.project_id,
        region=connection.region,
        configuration_set=infrastructure.configuration_set,
    )
    return connection


async def teardown_events(
    session: AsyncSession,
    provisioner_factory: ProvisionerFactory,
    connection: AWSConnection,
) -> bool:
    """Remove the infrastructure, unless another project still needs it.

    Returns whether anything was removed at AWS. The local columns are cleared
    either way - this project has stopped using it, which is true regardless of
    who else has not.
    """
    if not connection.events_enabled:
        return False

    others = await count_other_users(session, connection)
    infrastructure = connection.event_infrastructure

    connection.clear_event_infrastructure()
    await session.flush()

    if others:
        logger.info(
            "events_kept_for_other_projects",
            project_id=connection.project_id,
            region=connection.region,
            others=others,
        )
        return False

    provisioner = provisioner_factory(connection.region)
    await provisioner.remove_events(infrastructure)

    logger.info("events_torn_down", project_id=connection.project_id, region=connection.region)
    return True


async def set_open_click_tracking(
    session: AsyncSession,
    provisioner_factory: ProvisionerFactory,
    connection: AWSConnection,
    *,
    enabled: bool,
) -> AWSConnection:
    """Turn link and open tracking on or off for this project.

    Stored even when there is no infrastructure yet, so the preference survives
    until events are set up rather than being silently forgotten.
    """
    connection.track_opens_and_clicks = enabled

    if connection.events_enabled:
        provisioner = provisioner_factory(connection.region)
        infrastructure = await provisioner.set_open_click_tracking(
            connection.event_infrastructure, enabled=enabled
        )
        connection.record_event_infrastructure(infrastructure)

    await session.flush()
    return connection
