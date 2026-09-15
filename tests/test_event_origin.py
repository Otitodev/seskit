"""Which connection an SNS topic speaks for (§15).

An SNS signature proves Amazon SNS emitted a message, and no more than that:
SNS signing keys are per-region and shared by every customer, so anyone with
an AWS account can have SNS sign whatever they publish to a topic of their own.
The receiver therefore has to check the topic as well as the signature. This
file holds the half that reads the topic.

The binding is region and account, not the topic ARN, and the reason is a
race: SNS's subscription confirmation arrives *before* provisioning has
finished, so the ARN is not yet stored when SNS calls back. The account id is
stored at connect time and is there. `count_other_users` already treats that
pair as the identity of a shared queue and topic, so this agrees with it.
"""

from __future__ import annotations

import pytest
from fakes.ses import ACCOUNT_ID
from seskit_core.events import TopicOrigin, connections_for_origin, parse_topic_arn
from seskit_core.models import AWSConnection, ConnectionStatus
from seskit_core.services import create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "eu-west-2"


# ----------------------------------------------------------------- parsing ---


def test_a_topic_arn_names_its_region_and_account() -> None:
    origin = parse_topic_arn("arn:aws:sns:eu-west-2:946299734251:seskit-events")

    assert origin == TopicOrigin(region="eu-west-2", account_id="946299734251")


def test_other_partitions_parse_too() -> None:
    """China and GovCloud have their own partitions; the shape is otherwise
    the same, and a receiver deployed there must not refuse its own topic.
    """
    assert parse_topic_arn("arn:aws-cn:sns:cn-north-1:123456789012:t") == TopicOrigin(
        region="cn-north-1", account_id="123456789012"
    )


@pytest.mark.parametrize(
    "arn",
    [
        "",
        "not-an-arn",
        "arn:aws:sqs:eu-west-2:946299734251:seskit-events",  # wrong service
        "arn:aws:sns:eu-west-2:946299734251",  # too few segments
        "arn:aws:sns:eu-west-2:946299734251:a:b",  # too many
        "arn:aws:sns::946299734251:seskit-events",  # no region
        "arn:aws:sns:eu-west-2:not-an-account:seskit-events",  # account not numeric
        "arn:aws:sns:eu-west-2::seskit-events",  # no account
    ],
)
def test_anything_that_is_not_a_topic_arn_is_no_origin(arn: str) -> None:
    """None, not a best guess. A malformed ARN is an origin nobody connected,
    and the caller treats it exactly as an unknown account.
    """
    assert parse_topic_arn(arn) is None


# ------------------------------------------------------------- resolution ---


async def _connected(
    session: AsyncSession,
    *,
    owner: str,
    account: str = ACCOUNT_ID,
    region: str = REGION,
    status: str = ConnectionStatus.CONNECTED.value,
) -> str:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    session.add(
        AWSConnection(
            project_id=project.id,
            region=region,
            aws_account_id=account,
            status=status,
            aws_access_key_id="AKIAEXAMPLE",
        )
    )
    await session.flush()
    return str(project.id)


async def test_a_connected_account_resolves_to_its_projects(db_session: AsyncSession) -> None:
    mine = await _connected(db_session, owner="one@example.com")

    found = await connections_for_origin(db_session, TopicOrigin(REGION, ACCOUNT_ID))

    assert [c.project_id for c in found] == [mine]


async def test_projects_sharing_an_account_all_resolve(db_session: AsyncSession) -> None:
    """One account, one region, one topic - several projects. An event from
    that topic may concern any of them, so all of them come back.
    """
    one = await _connected(db_session, owner="one@example.com")
    two = await _connected(db_session, owner="two@example.com")

    found = await connections_for_origin(db_session, TopicOrigin(REGION, ACCOUNT_ID))

    assert {c.project_id for c in found} == {one, two}


async def test_an_account_nobody_connected_resolves_to_nothing(
    db_session: AsyncSession,
) -> None:
    """The finding, in one line. A stranger's topic is a stranger's account,
    and no connection here names it.
    """
    await _connected(db_session, owner="one@example.com")

    found = await connections_for_origin(db_session, TopicOrigin(REGION, "999999999999"))

    assert found == []


async def test_the_same_account_in_another_region_is_a_different_origin(
    db_session: AsyncSession,
) -> None:
    """Topics are regional. A connection in eu-west-2 says nothing about a
    topic in us-east-1, even in the same account.
    """
    await _connected(db_session, owner="one@example.com", region="eu-west-2")

    found = await connections_for_origin(db_session, TopicOrigin("us-east-1", ACCOUNT_ID))

    assert found == []


async def test_a_broken_connection_still_owns_its_messages(db_session: AsyncSession) -> None:
    """A rotated key puts a connection in `error`. SES keeps publishing that
    project's events regardless, and refusing them would lose real delivery
    history while the key is being fixed. The account id was verified when
    the project connected; a stranger cannot put a row in this table.
    """
    broken = await _connected(
        db_session, owner="one@example.com", status=ConnectionStatus.ERROR.value
    )

    found = await connections_for_origin(db_session, TopicOrigin(REGION, ACCOUNT_ID))

    assert [c.project_id for c in found] == [broken]
