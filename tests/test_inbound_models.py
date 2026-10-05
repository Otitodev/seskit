"""The storage for received mail, against a real database.

Postgres rather than a stub, because what is worth testing here - a partial
unique index, two uniqueness rules, a cascade - is enforced by the database and
does not exist anywhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from seskit_core.ids import IDPrefix, has_prefix
from seskit_core.models import (
    AWSConnection,
    Identity,
    InboundEmail,
    Project,
    User,
)
from seskit_core.providers import InboundInfrastructure
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


async def _project(session: AsyncSession, email: str = "owner@example.com") -> Project:
    user = User(email=email, password_hash="x", is_owner=True)
    session.add(user)
    await session.flush()
    project = Project(user_id=user.id, name="Default")
    session.add(project)
    await session.flush()
    return project


def _identity(project: Project, value: str = "example.com", *, rule: str | None = None) -> Identity:
    return Identity(
        project_id=project.id,
        identity_type="domain",
        value=value,
        region="us-east-1",
        inbound_rule_name=rule,
    )


def _inbound(project: Project, **overrides: object) -> InboundEmail:
    fields: dict[str, object] = {
        "project_id": project.id,
        "domain": "example.com",
        "provider_message_id": "ses-msg-1",
        "provider_event_id": "sns-1",
        "storage_bucket": "seskit-inbound-1",
        "storage_key": "seskit-example-com/ses-msg-1",
        "received_at": NOW,
    }
    return InboundEmail(**(fields | overrides))


# ------------------------------------------------------------ the message ---


async def test_a_received_message_gets_a_prefixed_id_and_empty_defaults(
    db_session: AsyncSession,
) -> None:
    project = await _project(db_session)
    message = _inbound(project)
    db_session.add(message)
    await db_session.flush()
    await db_session.refresh(message)

    assert has_prefix(message.id, IDPrefix.INBOUND_EMAIL)
    assert message.id.startswith("inbound_")
    assert message.subject == ""
    assert message.to_addresses == []
    assert message.attachments == []
    assert message.has_attachments is False
    # Set by the database, so a row written outside the ORM is stamped too.
    assert message.created_at is not None
    # A new message has not hit a ceiling and was not unreadable.
    assert message.truncated is False
    assert message.parse_failed is False
    # Verdicts the notification did not carry are unknown, which is not PASS.
    assert message.spf_verdict is None
    assert message.dmarc_policy is None


async def test_the_same_stored_object_cannot_be_recorded_twice(
    db_session: AsyncSession,
) -> None:
    """SES can announce one stored message again under a different SNS id, which
    only the storage location notices. A second row for it is a duplicate in
    the inbox.
    """
    project = await _project(db_session)
    db_session.add(_inbound(project, provider_event_id="sns-1"))
    await db_session.flush()

    db_session.add(_inbound(project, provider_event_id="sns-2"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_the_same_notification_cannot_be_recorded_twice(
    db_session: AsyncSession,
) -> None:
    """SNS is at-least-once. The same notification arrives twice sooner or later,
    and its message id is what says so.
    """
    project = await _project(db_session)
    db_session.add(_inbound(project, storage_key="seskit-example-com/a"))
    await db_session.flush()

    db_session.add(_inbound(project, storage_key="seskit-example-com/b"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_notification_with_no_id_is_still_recorded(
    db_session: AsyncSession,
) -> None:
    """NULL is not equal to NULL, so two messages that arrived without an SNS id
    do not collide. Recorded rather than dropped, which is the point of the
    column being nullable.
    """
    project = await _project(db_session)
    db_session.add(_inbound(project, provider_event_id=None, storage_key="seskit-x/a"))
    db_session.add(_inbound(project, provider_event_id=None, storage_key="seskit-x/b"))

    await db_session.flush()


async def test_deleting_a_project_deletes_what_it_received(db_session: AsyncSession) -> None:
    project = await _project(db_session)
    db_session.add(_inbound(project))
    await db_session.flush()

    await db_session.delete(project)
    await db_session.flush()

    total = await db_session.scalar(select(func.count()).select_from(InboundEmail))
    assert total == 0


async def test_received_mail_survives_the_domain_being_removed(
    db_session: AsyncSession,
) -> None:
    """Removing a domain stops future mail. It must not take what was already
    received with it, which is why a message holds the domain as text and not a
    reference to the identity.
    """
    project = await _project(db_session)
    identity = _identity(project, rule="seskit-example-com")
    db_session.add(identity)
    db_session.add(_inbound(project))
    await db_session.flush()

    await db_session.delete(identity)
    await db_session.flush()

    total = await db_session.scalar(select(func.count()).select_from(InboundEmail))
    assert total == 1


# ------------------------------------------------- one receiver per domain ---


async def test_two_identities_may_share_a_value_while_neither_receives(
    db_session: AsyncSession,
) -> None:
    """The index is partial. Two projects adopting one verified domain is normal,
    and most identities receive nothing.
    """
    first = await _project(db_session, "a@example.com")
    second = await _project(db_session, "b@example.com")
    db_session.add(_identity(first))
    db_session.add(_identity(second))

    await db_session.flush()


async def test_a_second_identity_may_share_a_value_with_one_that_receives(
    db_session: AsyncSession,
) -> None:
    first = await _project(db_session, "a@example.com")
    second = await _project(db_session, "b@example.com")
    db_session.add(_identity(first, rule="seskit-example-com"))
    db_session.add(_identity(second))

    await db_session.flush()


async def test_a_domain_cannot_receive_for_two_identities(db_session: AsyncSession) -> None:
    """An MX record names one endpoint in one region, so two identities both
    receiving for a domain cannot both be right - mail would belong to whichever
    was looked up first. The database refuses it, so no race in the service
    layer can produce it.
    """
    first = await _project(db_session, "a@example.com")
    second = await _project(db_session, "b@example.com")
    db_session.add(_identity(first, rule="seskit-one"))
    await db_session.flush()

    db_session.add(_identity(second, rule="seskit-two"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_identity_defaults_to_not_receiving(db_session: AsyncSession) -> None:
    project = await _project(db_session)
    identity = _identity(project)
    db_session.add(identity)
    await db_session.flush()
    await db_session.refresh(identity)

    assert identity.receives_mail is False
    assert identity.inbound_rule_name is None
    assert identity.inbound_rule_set_created is False


async def test_an_identity_with_a_rule_receives_mail(db_session: AsyncSession) -> None:
    project = await _project(db_session)
    identity = _identity(project, rule="seskit-example-com")
    db_session.add(identity)
    await db_session.flush()

    assert identity.receives_mail is True


# -------------------------------------------------- the connection's side ---


def test_a_fresh_connection_has_no_receiving_plumbing() -> None:
    connection = AWSConnection()

    assert connection.inbound_enabled is False
    assert not connection.inbound_infrastructure.exists


def test_recorded_infrastructure_reads_back_unchanged() -> None:
    connection = AWSConnection()
    infrastructure = InboundInfrastructure(
        bucket="seskit-inbound-1",
        topic_arn="arn:aws:sns:us-east-1:111122223333:seskit-inbound",
        queue_url="https://sqs.us-east-1.amazonaws.com/111122223333/seskit-inbound",
        queue_arn="arn:aws:sqs:us-east-1:111122223333:seskit-inbound",
        subscription_arn="arn:aws:sns:us-east-1:111122223333:seskit-inbound:abc",
    )

    connection.record_inbound_infrastructure(infrastructure)

    assert connection.inbound_infrastructure == infrastructure
    assert connection.inbound_enabled is True


def test_a_kept_bucket_does_not_mean_mail_is_being_received() -> None:
    """After teardown a bucket that still holds mail stays recorded, so a later
    setup finds it again. It must not read as receiving: nothing is listening.
    """
    connection = AWSConnection()
    connection.record_inbound_infrastructure(InboundInfrastructure(bucket="seskit-inbound-1"))

    assert connection.inbound_infrastructure.exists is True
    assert connection.inbound_enabled is False


def test_empty_strings_are_stored_as_null() -> None:
    connection = AWSConnection()
    connection.record_inbound_infrastructure(InboundInfrastructure())

    assert connection.inbound_bucket is None
    assert connection.inbound_topic_arn is None
    assert connection.inbound_queue_url is None
