"""Setting up and removing the plumbing for received mail.

The refcount is why this file exists, and it is the same trap ``test_event_setup``
documents. Two projects in one AWS account and region share the bucket, the
topic and the queue, so tearing them down when the first project's last domain
goes would stop the second project's mail - and nothing on either screen would
say why.

The second reason is the order of operations. AWS is touched first on every
removal and the row second, because the other way round a failure leaves a row
saying "not receiving" while the rule is still in somebody's account.
"""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from fakes.ses import TEST_SECRET_KEY, FakeProviderFactory, connect_project
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import AWSConnection, Identity
from seskit_core.providers import (
    AWSCredentials,
    InboundInfrastructure,
    InboundProvisioner,
    InboundRule,
)
from seskit_core.services import (
    bucket_name_for,
    count_other_receivers,
    create_project,
    disconnect_aws,
    distinct_inbound_queues,
    register_user,
    remove_identity,
    rule_name_for,
    setup_receiving,
    teardown_all_receiving,
    teardown_receiving,
)
from seskit_core.services.inbound import RULE_NAME_PREFIX
from seskit_provider_aws_ses import RULE_NAME_PREFIX as ADAPTER_RULE_NAME_PREFIX
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
PREFIX = "seskit"
DOMAIN = "example.com"


class FakeInbound:
    """Records what it was asked to build and to remove.

    Shared state is class-level because the factory builds a new instance per
    call and a test has to see every call across a request, which is also what
    makes the refcount observable at all.
    """

    calls: ClassVar[list[tuple[str, object]]] = []
    #: What ``remove_inbound`` reports: whether the bucket went too.
    bucket_removed: ClassVar[bool] = True
    #: Raised by ``remove_inbound_rule`` when set.
    remove_rule_error: ClassVar[APIError | None] = None
    #: Raised by ``provision_inbound`` when set, to stand in for AWS refusing -
    #: a missing permission, say.
    setup_error: ClassVar[APIError | None] = None
    #: Raised by ``add_inbound_rule`` alone, after the plumbing was built.
    rule_error: ClassVar[APIError | None] = None

    def __init__(self, region: str, credentials: AWSCredentials | None = None) -> None:
        self.region = region
        self.credentials = credentials

    async def provision_inbound(
        self,
        *,
        bucket_name: str,
        topic_name: str,
        queue_name: str,
        retention_days: int,
    ) -> InboundInfrastructure:
        if FakeInbound.setup_error is not None:
            raise FakeInbound.setup_error
        FakeInbound.calls.append(
            ("provision", {"bucket": bucket_name, "retention_days": retention_days})
        )
        return InboundInfrastructure(
            bucket=bucket_name,
            topic_arn=f"arn:aws:sns:{self.region}:123456789012:{topic_name}",
            queue_url=f"https://sqs.{self.region}.amazonaws.com/123456789012/{queue_name}",
            queue_arn=f"arn:aws:sqs:{self.region}:123456789012:{queue_name}",
            subscription_arn=f"arn:aws:sns:{self.region}:123456789012:{topic_name}:sub",
        )

    async def add_inbound_rule(
        self, infrastructure: InboundInfrastructure, *, domain: str, rule_name: str
    ) -> InboundRule:
        if FakeInbound.rule_error is not None:
            raise FakeInbound.rule_error
        FakeInbound.calls.append(("add_rule", domain))
        return InboundRule(name=rule_name, rule_set="seskit-inbound", created_rule_set=True)

    async def remove_inbound_rule(self, rule: InboundRule) -> None:
        if FakeInbound.remove_rule_error is not None:
            raise FakeInbound.remove_rule_error
        FakeInbound.calls.append(("remove_rule", rule))

    async def remove_inbound(self, infrastructure: InboundInfrastructure) -> bool:
        FakeInbound.calls.append(("remove", infrastructure))
        return FakeInbound.bucket_removed


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeInbound.calls = []
    FakeInbound.bucket_removed = True
    FakeInbound.remove_rule_error = None
    FakeInbound.setup_error = None
    FakeInbound.rule_error = None


def factory(region: str, credentials: AWSCredentials) -> InboundProvisioner:
    return FakeInbound(region, credentials)


def _kinds() -> list[str]:
    return [kind for kind, _ in FakeInbound.calls]


async def _project_connection(
    session: AsyncSession,
    *,
    email: str,
    region: str = REGION,
    account: str | None = None,
) -> AWSConnection:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=f"P-{email}")
    connection: AWSConnection = await connect_project(session, project.id, region=region)
    if account is not None:
        connection.aws_account_id = account
        await session.flush()
    return connection


async def _domain(
    session: AsyncSession,
    connection: AWSConnection,
    value: str = DOMAIN,
    *,
    verified: bool = True,
    kind: str = "domain",
    region: str | None = None,
) -> Identity:
    identity = Identity(
        project_id=connection.project_id,
        identity_type=kind,
        value=value,
        region=region or connection.region,
        verification_status="success" if verified else "pending",
    )
    session.add(identity)
    await session.flush()
    return identity


async def _enable(
    session: AsyncSession,
    connection: AWSConnection,
    identity: Identity,
    *,
    retention_days: int = 30,
) -> None:
    await setup_receiving(
        session,
        factory,
        connection,
        identity,
        resource_prefix=PREFIX,
        retention_days=retention_days,
        secret_key=TEST_SECRET_KEY,
    )


# ------------------------------------------------------------------- names ---


def test_the_rule_prefix_here_is_the_one_the_bucket_policy_admits() -> None:
    """Core cannot import the adapter to share the constant, so this holds the
    two together. A rule named otherwise would be accepted by SES and its writes
    refused by the bucket: mail lost, with no error anywhere.
    """
    assert RULE_NAME_PREFIX == ADAPTER_RULE_NAME_PREFIX


def test_a_bucket_name_carries_the_account_and_region() -> None:
    name = bucket_name_for("seskit", account_id="123456789012", region="eu-west-1")

    assert name == "seskit-inbound-123456789012-eu-west-1"
    assert len(name) <= 63


def test_a_bucket_name_is_lower_cased() -> None:
    assert bucket_name_for("SESKit", account_id="1", region="us-east-1").islower()


@pytest.mark.parametrize("prefix", ["x" * 60, "has space", "under_score", "-leading"])
def test_a_prefix_a_bucket_name_cannot_hold_is_refused_with_a_useful_message(
    prefix: str,
) -> None:
    with pytest.raises(APIError) as caught:
        bucket_name_for(prefix, account_id="123456789012", region="us-east-1")

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert "EVENT_RESOURCE_PREFIX" in caught.value.message


@pytest.mark.parametrize(
    "domain", ["example.com", "a.b.c.example.co.uk", "xn--bcher-kva.example", "a" * 63 + ".com"]
)
def test_a_rule_name_is_one_ses_will_accept(domain: str) -> None:
    name = rule_name_for(PREFIX, domain)

    assert name.startswith(RULE_NAME_PREFIX)
    assert 1 <= len(name) <= 64
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?", name)


def test_a_rule_name_is_stable_and_distinguishes_domains_and_instances() -> None:
    assert rule_name_for(PREFIX, "example.com") == rule_name_for(PREFIX, "example.com")
    assert rule_name_for(PREFIX, "example.com") != rule_name_for(PREFIX, "example.net")
    # Two instances sharing an AWS account must not collide on a rule name.
    assert rule_name_for("seskit-aaa", "example.com") != rule_name_for("seskit-bbb", "example.com")


# ------------------------------------------------------------------- setup ---


async def test_setup_records_the_plumbing_and_the_rule(db_session: AsyncSession) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)

    await _enable(db_session, connection, identity, retention_days=45)

    assert connection.inbound_enabled is True
    assert connection.inbound_bucket == f"seskit-inbound-{connection.aws_account_id}-{REGION}"
    assert identity.receives_mail is True
    assert identity.inbound_rule_name == rule_name_for(PREFIX, DOMAIN)
    assert identity.inbound_rule_set == "seskit-inbound"
    assert identity.inbound_rule_set_created is True
    provision = dict(FakeInbound.calls)["provision"]
    assert provision["retention_days"] == 45  # type: ignore[index]


async def test_setup_twice_leaves_one_rule_and_the_same_state(db_session: AsyncSession) -> None:
    """How a user repairs a rule they deleted in the console, and how a changed
    retention is applied.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)

    await _enable(db_session, connection, identity)
    first = (connection.inbound_infrastructure, identity.inbound_rule_name)
    await _enable(db_session, connection, identity, retention_days=7)

    assert (connection.inbound_infrastructure, identity.inbound_rule_name) == first


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"kind": "email_address", "value": "me@example.com"}, ErrorType.INVALID_REQUEST),
        ({"verified": False}, ErrorType.DOMAIN_NOT_VERIFIED),
        ({"region": "eu-west-1"}, ErrorType.INVALID_REQUEST),
    ],
)
async def test_setup_refuses_what_cannot_receive_before_touching_aws(
    db_session: AsyncSession, kwargs: dict[str, object], error_type: ErrorType
) -> None:
    """An address has no MX record, an unverified domain is not ours yet, and a
    domain in another region than the connection would receive nowhere.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    value = str(kwargs.pop("value", DOMAIN))
    identity = await _domain(db_session, connection, value, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(APIError) as caught:
        await _enable(db_session, connection, identity)

    assert caught.value.error_type is error_type
    assert FakeInbound.calls == []
    assert identity.receives_mail is False


async def test_a_domain_already_receiving_elsewhere_is_refused_before_aws_is_touched(
    db_session: AsyncSession,
) -> None:
    """One receiver per domain. The refusal has to come before anything is built:
    a loser that had already created a rule in somebody's account would leave it
    there with nothing recording it.
    """
    first = await _project_connection(db_session, email="a@example.com")
    second = await _project_connection(db_session, email="b@example.com")
    mine = await _domain(db_session, first)
    theirs = await _domain(db_session, second)
    await _enable(db_session, first, mine)
    FakeInbound.calls = []

    with pytest.raises(APIError) as caught:
        await _enable(db_session, second, theirs)

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert "already receives" in caught.value.message
    assert FakeInbound.calls == []
    assert theirs.receives_mail is False
    # The refusal says nothing about whose it is.
    assert first.project_id not in caught.value.message
    assert mine.receives_mail is True


# --------------------------------------------------- when AWS refuses ---


async def test_a_refusal_from_aws_leaves_the_domain_not_receiving(
    db_session: AsyncSession,
) -> None:
    """The claim is written before AWS is called, so that the unique index can
    decide a race. If AWS then refuses, the identity must not say it receives: the
    routes that call this do not roll back, so the same render would show
    "Receiving: on" beside the reason it is not.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    FakeInbound.setup_error = APIError(
        ErrorType.AUTHORIZATION_FAILED, "not permitted to call s3:CreateBucket"
    )

    with pytest.raises(APIError):
        await _enable(db_session, connection, identity)

    assert identity.receives_mail is False
    assert identity.inbound_rule_name is None
    assert identity.inbound_rule_set is None
    assert identity.inbound_rule_set_created is False
    # And it is not left holding the domain against a second attempt.
    FakeInbound.setup_error = None
    await _enable(db_session, connection, identity)
    assert identity.receives_mail is True


async def test_a_refusal_after_the_plumbing_exists_keeps_what_was_built(
    db_session: AsyncSession,
) -> None:
    """The bucket, topic and queue are really in the account. Forgetting them
    would leave resources teardown can never find; the identity is what is given
    back, not the record of what was created.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    FakeInbound.rule_error = APIError(
        ErrorType.AUTHORIZATION_FAILED, "not permitted to call ses:CreateReceiptRule"
    )

    with pytest.raises(APIError):
        await _enable(db_session, connection, identity)

    assert identity.receives_mail is False
    assert connection.inbound_enabled is True


async def test_a_failed_repair_puts_back_the_rule_that_was_there(
    db_session: AsyncSession,
) -> None:
    """Running setup again on a domain that already receives must not leave it
    blank if the second attempt fails. It was receiving before; it still is.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    before = (
        identity.inbound_rule_name,
        identity.inbound_rule_set,
        identity.inbound_rule_set_created,
    )
    FakeInbound.setup_error = APIError(ErrorType.PROVIDER_ERROR, "AWS is down")

    with pytest.raises(APIError):
        await _enable(db_session, connection, identity)

    after = (
        identity.inbound_rule_name,
        identity.inbound_rule_set,
        identity.inbound_rule_set_created,
    )
    assert after == before
    assert identity.receives_mail is True


# ---------------------------------------------------------------- teardown ---


async def test_teardown_removes_the_rule_then_the_plumbing_when_it_was_the_last(
    db_session: AsyncSession,
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, connection, identity, secret_key=TEST_SECRET_KEY)

    assert _kinds() == ["remove_rule", "remove"]
    assert identity.receives_mail is False
    assert identity.inbound_rule_set is None
    assert identity.inbound_rule_set_created is False
    assert connection.inbound_enabled is False
    assert connection.inbound_bucket is None


async def test_the_rule_that_is_removed_is_the_one_that_was_created(
    db_session: AsyncSession,
) -> None:
    """Recorded rather than derived: the set is whichever the account had active
    at the time, and no name can reconstruct that.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    expected = InboundRule(
        name=identity.inbound_rule_name or "", rule_set="seskit-inbound", created_rule_set=True
    )
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, connection, identity, secret_key=TEST_SECRET_KEY)

    assert dict(FakeInbound.calls)["remove_rule"] == expected


async def test_plumbing_stays_while_another_domain_in_the_project_still_receives(
    db_session: AsyncSession,
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    one = await _domain(db_session, connection, "one.example")
    two = await _domain(db_session, connection, "two.example")
    await _enable(db_session, connection, one)
    await _enable(db_session, connection, two)
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, connection, one, secret_key=TEST_SECRET_KEY)

    assert _kinds() == ["remove_rule"]
    assert connection.inbound_enabled is True
    assert two.receives_mail is True


async def test_plumbing_stays_while_another_project_in_the_account_still_receives(
    db_session: AsyncSession,
) -> None:
    """The reason this file exists. Both projects share one bucket, topic and
    queue; removing them when the first project's last domain goes would stop the
    second project's mail, and nothing would report it.
    """
    first = await _project_connection(db_session, email="a@example.com")
    second = await _project_connection(db_session, email="b@example.com")
    mine = await _domain(db_session, first, "mine.example")
    theirs = await _domain(db_session, second, "theirs.example")
    await _enable(db_session, first, mine)
    await _enable(db_session, second, theirs)
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, first, mine, secret_key=TEST_SECRET_KEY)

    assert _kinds() == ["remove_rule"]
    assert "remove" not in _kinds()
    # This project has stopped using the plumbing, whoever else has not...
    assert first.inbound_enabled is False
    # ...and the other project still has everything it needs.
    assert second.inbound_enabled is True
    assert theirs.receives_mail is True


async def test_the_last_project_to_stop_removes_the_shared_plumbing(
    db_session: AsyncSession,
) -> None:
    first = await _project_connection(db_session, email="a@example.com")
    second = await _project_connection(db_session, email="b@example.com")
    mine = await _domain(db_session, first, "mine.example")
    theirs = await _domain(db_session, second, "theirs.example")
    await _enable(db_session, first, mine)
    await _enable(db_session, second, theirs)
    await teardown_receiving(db_session, factory, first, mine, secret_key=TEST_SECRET_KEY)
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, second, theirs, secret_key=TEST_SECRET_KEY)

    assert _kinds() == ["remove_rule", "remove"]


async def test_a_project_in_another_account_does_not_share_the_plumbing(
    db_session: AsyncSession,
) -> None:
    """Matched on account and region. Another account's project is not a user of
    this bucket, and counting it would leave this one behind for ever.
    """
    first = await _project_connection(db_session, email="a@example.com")
    other_account = await _project_connection(
        db_session, email="b@example.com", account="999999999999"
    )
    other_region = await _project_connection(db_session, email="c@example.com", region="eu-west-1")
    for connection, name in ((other_account, "b.example"), (other_region, "c.example")):
        await _enable(db_session, connection, await _domain(db_session, connection, name))
    mine = await _domain(db_session, first, "mine.example")
    await _enable(db_session, first, mine)

    assert await count_other_receivers(db_session, first) == 0
    FakeInbound.calls = []

    await teardown_receiving(db_session, factory, first, mine, secret_key=TEST_SECRET_KEY)

    assert _kinds() == ["remove_rule", "remove"]


async def test_a_bucket_that_still_holds_mail_stays_recorded(db_session: AsyncSession) -> None:
    """The provider keeps it. If the row forgot the name, a bucket full of
    somebody's mail would sit in their account with nothing to say it was ours;
    kept, a later setup finds it again.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    bucket = connection.inbound_bucket
    FakeInbound.bucket_removed = False

    await teardown_receiving(db_session, factory, connection, identity, secret_key=TEST_SECRET_KEY)

    assert connection.inbound_bucket == bucket
    # Kept is not receiving: nothing is listening.
    assert connection.inbound_enabled is False
    assert connection.inbound_topic_arn is None


async def test_tearing_down_a_domain_that_does_not_receive_is_a_no_op(
    db_session: AsyncSession,
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)

    await teardown_receiving(db_session, factory, connection, identity, secret_key=TEST_SECRET_KEY)

    assert FakeInbound.calls == []


async def test_a_failed_removal_leaves_the_row_saying_it_still_receives(
    db_session: AsyncSession,
) -> None:
    """AWS first, the row second. The other way round, a removal that fails
    halfway leaves a row saying "not receiving" while the rule is still in
    somebody's account, and nothing left to name it.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    FakeInbound.remove_rule_error = APIError(ErrorType.PROVIDER_ERROR, "AWS said no")

    with pytest.raises(APIError):
        await teardown_receiving(
            db_session, factory, connection, identity, secret_key=TEST_SECRET_KEY
        )

    assert identity.receives_mail is True
    assert identity.inbound_rule_set == "seskit-inbound"
    assert connection.inbound_enabled is True


async def test_teardown_all_removes_every_domains_rule_and_the_plumbing_once(
    db_session: AsyncSession,
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    one = await _domain(db_session, connection, "one.example")
    two = await _domain(db_session, connection, "two.example")
    await _enable(db_session, connection, one)
    await _enable(db_session, connection, two)
    FakeInbound.calls = []

    await teardown_all_receiving(db_session, factory, connection, secret_key=TEST_SECRET_KEY)

    assert _kinds().count("remove_rule") == 2
    assert _kinds().count("remove") == 1
    assert one.receives_mail is False
    assert two.receives_mail is False
    assert connection.inbound_enabled is False


# ------------------------------------------------------------------- hooks ---


async def test_removing_a_receiving_domain_removes_its_rule_first(
    db_session: AsyncSession,
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    FakeInbound.calls = []

    await remove_identity(
        db_session,
        FakeProviderFactory(),
        identity,
        secret_key=TEST_SECRET_KEY,
        inbound_factory=factory,
    )

    assert "remove_rule" in _kinds()


async def test_removing_a_receiving_domain_without_a_factory_refuses(
    db_session: AsyncSession,
) -> None:
    """Deleting the row would strand the rule: once it is gone nothing can say
    which receipt rule in the account was ours.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)

    with pytest.raises(ValueError, match="receives mail"):
        await remove_identity(
            db_session, FakeProviderFactory(), identity, secret_key=TEST_SECRET_KEY
        )


async def test_removing_a_domain_that_does_not_receive_needs_no_factory(
    db_session: AsyncSession,
) -> None:
    """Every call before this phase is in this state, and must keep working."""
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)

    await remove_identity(db_session, FakeProviderFactory(), identity, secret_key=TEST_SECRET_KEY)

    assert FakeInbound.calls == []


async def test_disconnecting_removes_receiving_before_the_key_is_gone(
    db_session: AsyncSession, redis_client: object
) -> None:
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)
    FakeInbound.calls = []

    await disconnect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        connection,
        secret_key=TEST_SECRET_KEY,
        inbound_factory=factory,
    )

    assert _kinds() == ["remove_rule", "remove"]


async def test_disconnecting_a_receiving_project_without_a_factory_refuses(
    db_session: AsyncSession, redis_client: object
) -> None:
    """Once the connection row is gone there is no key left to remove the rule
    with, so refusing is the only way not to abandon it.
    """
    connection = await _project_connection(db_session, email="a@example.com")
    identity = await _domain(db_session, connection)
    await _enable(db_session, connection, identity)

    with pytest.raises(ValueError, match="receives mail"):
        await disconnect_aws(
            db_session,
            redis_client,  # type: ignore[arg-type]
            connection,
            secret_key=TEST_SECRET_KEY,
        )


async def test_disconnecting_a_project_that_does_not_receive_still_works(
    db_session: AsyncSession, redis_client: object
) -> None:
    """Every connection made before this phase is in this state."""
    connection = await _project_connection(db_session, email="a@example.com")

    await disconnect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        connection,
        secret_key=TEST_SECRET_KEY,
    )

    assert FakeInbound.calls == []


# ----------------------------------------------------------------- polling ---


async def test_projects_sharing_an_account_and_region_share_one_queue(
    db_session: AsyncSession,
) -> None:
    """Polling once per project would mean several consumers racing for the same
    messages. One queue, one reader, and it knows every project it speaks for.
    """
    first = await _project_connection(db_session, email="a@example.com")
    second = await _project_connection(db_session, email="b@example.com")
    await _enable(db_session, first, await _domain(db_session, first, "a.example"))
    await _enable(db_session, second, await _domain(db_session, second, "b.example"))

    queues = await distinct_inbound_queues(db_session, secret_key=TEST_SECRET_KEY)

    assert len(queues) == 1
    assert queues[0].project_ids == {first.project_id, second.project_id}
    assert queues[0].bucket == first.inbound_bucket
    assert queues[0].region == REGION


async def test_a_project_with_nothing_set_up_is_not_polled(db_session: AsyncSession) -> None:
    await _project_connection(db_session, email="a@example.com")

    assert await distinct_inbound_queues(db_session, secret_key=TEST_SECRET_KEY) == []


async def test_a_key_that_cannot_be_read_skips_the_queue_instead_of_raising(
    db_session: AsyncSession,
) -> None:
    """One project with a stale key must not stop every other project's mail."""
    connection = await _project_connection(db_session, email="a@example.com")
    await _enable(db_session, connection, await _domain(db_session, connection))

    assert await distinct_inbound_queues(db_session, secret_key="a-different-secret") == []
