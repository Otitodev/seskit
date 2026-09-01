"""Setting up and tearing down events for a project (§15).

The refcount is why this file exists. §9 puts one AWS identity behind the whole
instance, so two projects in the same region share an account - and if they
share a region they share the queue, the topic and the configuration set,
because those are named per region, not per project. Tearing down when the
second project disconnects would take the first project's events with it, and
nothing would report an error: SES would simply stop publishing and deliveries
would go quiet.

The same trap as Phase 5's identity refcount, which was found against real SES
rather than in a test - so this time it is a test.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import AWSConnection, ConnectionStatus
from seskit_core.providers import EventInfrastructure
from seskit_core.services import (
    create_project,
    disconnect_aws,
    register_user,
    set_open_click_tracking,
    setup_events,
    teardown_events,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
ACCOUNT = "123456789012"


class FakeProvisioner:
    """Records what it was asked to build, and what it was asked to remove."""

    #: Shared across instances, because the factory builds a new one per call
    #: and the test needs to see every call - which is also what makes the
    #: refcount observable at all.
    calls: ClassVar[list[tuple[str, object]]] = []

    def __init__(self, region: str, *, error: APIError | None = None) -> None:
        self.region = region
        self.error = error

    async def provision_events(
        self,
        *,
        queue_name: str,
        topic_name: str,
        configuration_set: str,
        https_endpoint: str | None = None,
        track_opens_and_clicks: bool = False,
    ) -> EventInfrastructure:
        if self.error is not None:
            raise self.error
        built = EventInfrastructure(
            configuration_set=configuration_set,
            topic_arn=f"arn:aws:sns:{self.region}:{ACCOUNT}:{topic_name}",
            queue_url=f"https://sqs.{self.region}.amazonaws.com/{ACCOUNT}/{queue_name}",
            queue_arn=f"arn:aws:sqs:{self.region}:{ACCOUNT}:{queue_name}",
            subscription_arn=f"arn:aws:sns:{self.region}:{ACCOUNT}:{topic_name}:sub",
            https_subscription_arn="https-sub" if https_endpoint else "",
            tracks_opens_and_clicks=track_opens_and_clicks,
        )
        FakeProvisioner.calls.append(("provision", built))
        return built

    async def remove_events(self, infrastructure: EventInfrastructure) -> None:
        FakeProvisioner.calls.append(("remove", infrastructure))

    async def set_open_click_tracking(
        self, infrastructure: EventInfrastructure, *, enabled: bool
    ) -> EventInfrastructure:
        FakeProvisioner.calls.append(("tracking", enabled))
        return EventInfrastructure(
            configuration_set=infrastructure.configuration_set,
            topic_arn=infrastructure.topic_arn,
            queue_url=infrastructure.queue_url,
            queue_arn=infrastructure.queue_arn,
            subscription_arn=infrastructure.subscription_arn,
            https_subscription_arn=infrastructure.https_subscription_arn,
            tracks_opens_and_clicks=enabled,
        )


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    FakeProvisioner.calls = []


def factory(region: str) -> FakeProvisioner:
    return FakeProvisioner(region)


def _removals() -> list[EventInfrastructure]:
    return [item for kind, item in FakeProvisioner.calls if kind == "remove"]  # type: ignore[misc]


async def _connection(
    session: AsyncSession, *, email: str, name: str, region: str = REGION
) -> AWSConnection:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=name)
    connection = AWSConnection(
        project_id=project.id,
        aws_account_id=ACCOUNT,
        region=region,
        status=ConnectionStatus.CONNECTED.value,
    )
    session.add(connection)
    await session.flush()
    return connection


# ------------------------------------------------------------------- setup ---


async def test_setup_records_what_was_created(db_session: AsyncSession) -> None:
    """Recorded rather than re-derived from names: teardown must remove exactly
    what was created, in an account SESKit does not own.
    """
    connection = await _connection(db_session, email="a@example.com", name="One")

    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    assert connection.events_enabled is True
    assert connection.configuration_set == "seskit"
    assert connection.event_queue_url
    assert connection.event_topic_arn
    assert connection.event_subscription_arn


async def test_setup_is_repeatable(db_session: AsyncSession) -> None:
    """How a user repairs infrastructure they deleted by hand in the console."""
    connection = await _connection(db_session, email="a@example.com", name="One")

    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )
    first = connection.event_topic_arn
    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    assert connection.event_topic_arn == first


# ---------------------------------------------------------------- refcount ---


async def test_teardown_removes_infrastructure_no_one_else_uses(
    db_session: AsyncSession,
) -> None:
    connection = await _connection(db_session, email="a@example.com", name="One")
    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    removed = await teardown_events(db_session, factory, connection)

    assert removed is True
    assert len(_removals()) == 1
    assert connection.events_enabled is False


async def test_teardown_leaves_infrastructure_another_project_shares(
    db_session: AsyncSession,
) -> None:
    """The test that matters.

    Two projects, one account, one region - therefore one queue and one topic.
    Removing them because *this* project disconnected would silently stop the
    other project's events, with nothing anywhere reporting an error.
    """
    mine = await _connection(db_session, email="a@example.com", name="One")
    theirs = await _connection(db_session, email="b@example.com", name="Two")
    for connection in (mine, theirs):
        await setup_events(
            db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
        )

    removed = await teardown_events(db_session, factory, mine)

    assert removed is False
    assert _removals() == []
    # This project has stopped using it, which is true regardless of who has not.
    assert mine.events_enabled is False
    assert theirs.events_enabled is True


async def test_a_project_in_another_region_does_not_hold_it_open(
    db_session: AsyncSession,
) -> None:
    """Different region means different physical queue and topic, so it is not
    a shared resource and keeping it would be litter.
    """
    mine = await _connection(db_session, email="a@example.com", name="One")
    elsewhere = await _connection(db_session, email="b@example.com", name="Two", region="eu-west-1")
    for connection in (mine, elsewhere):
        await setup_events(
            db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
        )

    removed = await teardown_events(db_session, factory, mine)

    assert removed is True


async def test_the_last_project_out_removes_it(db_session: AsyncSession) -> None:
    """The other half of the refcount: shared infrastructure must not become
    permanent just because it was once shared.
    """
    mine = await _connection(db_session, email="a@example.com", name="One")
    theirs = await _connection(db_session, email="b@example.com", name="Two")
    for connection in (mine, theirs):
        await setup_events(
            db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
        )

    await teardown_events(db_session, factory, mine)
    removed = await teardown_events(db_session, factory, theirs)

    assert removed is True
    assert len(_removals()) == 1


async def test_teardown_without_infrastructure_does_nothing(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, email="a@example.com", name="One")

    assert await teardown_events(db_session, factory, connection) is False
    assert _removals() == []


# -------------------------------------------------------------- disconnect ---


async def test_disconnecting_removes_the_infrastructure(
    db_session: AsyncSession, redis_client: object
) -> None:
    """SESKit created these unprompted. Leaving them behind is litter the user
    did not ask for and cannot easily identify - three consoles, no owner.
    """
    connection = await _connection(db_session, email="a@example.com", name="One")
    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    await disconnect_aws(db_session, redis_client, connection, provisioner_factory=factory)  # type: ignore[arg-type]

    assert len(_removals()) == 1


async def test_disconnecting_without_a_provisioner_refuses(
    db_session: AsyncSession, redis_client: object
) -> None:
    """Rather than silently dropping the row and stranding a queue, a topic and
    a configuration set in someone's account with nothing left to say where
    they came from.
    """
    connection = await _connection(db_session, email="a@example.com", name="One")
    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    with pytest.raises(ValueError):
        await disconnect_aws(db_session, redis_client, connection)  # type: ignore[arg-type]


async def test_disconnecting_a_project_without_events_still_works(
    db_session: AsyncSession, redis_client: object
) -> None:
    """Every connection made before this phase is in this state."""
    connection = await _connection(db_session, email="a@example.com", name="One")

    await disconnect_aws(db_session, redis_client, connection)  # type: ignore[arg-type]


# ---------------------------------------------------------------- tracking ---


async def test_tracking_is_off_until_asked_for(db_session: AsyncSession) -> None:
    """It rewrites every link in the customer's mail and adds a pixel. That is
    a visible change to their product, and it is theirs to agree to.
    """
    connection = await _connection(db_session, email="a@example.com", name="One")

    assert connection.track_opens_and_clicks is False

    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    assert connection.track_opens_and_clicks is False


async def test_turning_tracking_on_reaches_the_provider(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, email="a@example.com", name="One")
    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    await set_open_click_tracking(db_session, factory, connection, enabled=True)

    assert ("tracking", True) in FakeProvisioner.calls
    assert connection.track_opens_and_clicks is True


async def test_the_preference_is_kept_before_events_exist(db_session: AsyncSession) -> None:
    """Otherwise a user who turns it on first and sets events up second finds it
    quietly off, with nothing to explain why.
    """
    connection = await _connection(db_session, email="a@example.com", name="One")

    await set_open_click_tracking(db_session, factory, connection, enabled=True)

    assert connection.track_opens_and_clicks is True
    # Nothing was called: there is no destination to update yet.
    assert FakeProvisioner.calls == []

    await setup_events(
        db_session, factory, connection, resource_prefix="seskit", configuration_set="seskit"
    )

    assert connection.track_opens_and_clicks is True


# ------------------------------------------------------------------ errors ---


async def test_a_failed_setup_records_nothing(db_session: AsyncSession) -> None:
    """A row claiming infrastructure that was never built would send every
    message through a configuration set SES does not have.
    """

    def failing(region: str) -> FakeProvisioner:
        return FakeProvisioner(region, error=APIError(ErrorType.PROVIDER_ERROR, "AWS said no."))

    connection = await _connection(db_session, email="a@example.com", name="One")

    with pytest.raises(APIError):
        await setup_events(
            db_session,
            failing,
            connection,
            resource_prefix="seskit",
            configuration_set="seskit",
        )

    assert connection.events_enabled is False
