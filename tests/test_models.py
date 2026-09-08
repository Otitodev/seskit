"""User and Project models, against a real database.

These run against Postgres rather than a mock because the things worth testing
here - a unique constraint, a cascade delete, a server-side default - are
enforced by the database and simply do not exist in a stub.
"""

from __future__ import annotations

import pytest
from seskit_core.ids import IDPrefix, has_prefix
from seskit_core.models import AWSConnection, Project, User, normalise_email
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_user(session: AsyncSession, email: str = "owner@example.com") -> User:
    user = User(email=normalise_email(email), password_hash="hashed", is_owner=True)
    session.add(user)
    await session.flush()
    return user


# ------------------------------------------------------------------- User ---


async def test_user_gets_a_prefixed_identifier(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)

    assert has_prefix(user.id, IDPrefix.USER)


async def test_timestamps_are_set_by_the_database(db_session: AsyncSession) -> None:
    """Server-side defaults, so rows written outside the ORM are stamped too."""
    user = await _make_user(db_session)
    await db_session.refresh(user)

    assert user.created_at is not None
    assert user.updated_at is not None
    assert user.created_at.tzinfo is not None


async def test_email_is_unique(db_session: AsyncSession) -> None:
    await _make_user(db_session, "taken@example.com")

    db_session.add(User(email="taken@example.com", password_hash="x"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_case_differing_emails_collide(db_session: AsyncSession) -> None:
    """Alice@Example.com and alice@example.com are one mailbox.

    Normalising on the way in is what makes the unique index meaningful; without
    it the database would happily accept both as separate accounts.
    """
    await _make_user(db_session, "Alice@Example.com")

    db_session.add(User(email=normalise_email("ALICE@example.COM"), password_hash="x"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  User@Example.COM  ", "user@example.com"),
        ("UPPER@EXAMPLE.COM", "upper@example.com"),
        ("already@lower.com", "already@lower.com"),
    ],
)
def test_normalise_email_trims_and_lowercases(raw: str, expected: str) -> None:
    assert normalise_email(raw) == expected


async def test_user_defaults_to_active_and_not_owner(db_session: AsyncSession) -> None:
    user = User(email="member@example.com", password_hash="x")
    db_session.add(user)
    await db_session.flush()

    assert user.is_active is True
    assert user.is_owner is False


def test_user_repr_omits_the_email() -> None:
    """repr reaches logs and exception reports, which must not carry addresses."""
    user = User(id="usr_01J", email="secret@example.com", password_hash="x")

    assert "secret@example.com" not in repr(user)
    assert "usr_01J" in repr(user)


# ---------------------------------------------------------------- Project ---


async def test_project_gets_a_prefixed_identifier(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    project = Project(user_id=user.id, name="Default")
    db_session.add(project)
    await db_session.flush()

    assert has_prefix(project.id, IDPrefix.PROJECT)


async def test_deleting_a_user_cascades_to_their_projects(db_session: AsyncSession) -> None:
    """Enforced by the database, not only the ORM.

    A delete issued from psql or a migration must not leave orphaned projects.
    """
    user = await _make_user(db_session)
    db_session.add(Project(user_id=user.id, name="Default"))
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    remaining = await db_session.scalar(select(func.count()).select_from(Project))
    assert remaining == 0


async def test_project_requires_an_existing_user(db_session: AsyncSession) -> None:
    db_session.add(Project(user_id="usr_does_not_exist", name="Orphan"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_user_can_hold_several_projects(db_session: AsyncSession) -> None:
    """Section 6: a user may have one or more projects."""
    user = await _make_user(db_session)
    db_session.add_all(
        [
            Project(user_id=user.id, name="Default"),
            Project(user_id=user.id, name="Staging"),
        ]
    )
    await db_session.flush()

    count = await db_session.scalar(
        select(func.count()).select_from(Project).where(Project.user_id == user.id)
    )
    assert count == 2


# ------------------------------------------------------------- isolation ---


async def test_each_test_starts_from_an_empty_database(db_session: AsyncSession) -> None:
    """Proves the rollback fixture works.

    Every test above inserts users; if any of that survived, this would fail and
    the whole suite would be quietly order-dependent.
    """
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


# ------------------------------------------------- the stored access key ---


@pytest.mark.parametrize(
    ("key", "shown"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "AKIA\u2026MPLE"),
        ("AKIAROTATEDKEY123456", "AKIA\u20263456"),
        ("AKIASHORT", "AKIASHORT"),
        ("", ""),
        (None, ""),
    ],
    ids=["typical", "another", "too-short-to-shorten", "empty", "unset"],
)
def test_the_access_key_is_shortened_for_a_screen(key: str | None, shown: str) -> None:
    """Not redaction - an access key id is an identifier, and AWS shows them in
    full in its own console. It is that a dashboard gets screenshotted, and the
    first four and last four answer the only question anyone asks of it.
    """
    assert AWSConnection(aws_access_key_id=key).access_key_display == shown


def test_two_keys_are_told_apart_by_what_is_shown() -> None:
    """The first four characters are always AKIA, so shortening to a prefix
    alone would render every key identically.
    """
    first = AWSConnection(aws_access_key_id="AKIAIOSFODNN7EXAMPLE").access_key_display
    second = AWSConnection(aws_access_key_id="AKIAIOSFODNN7DIFFER").access_key_display

    assert first != second


def test_a_connection_without_a_key_is_not_usable() -> None:
    """Every row from before Phase 14 is this. The migration marks them broken,
    and this is the belt to that braces.
    """
    assert AWSConnection(aws_access_key_id=None).has_credentials is False
    assert AWSConnection(aws_access_key_id="AKIA123").has_credentials is False


def test_a_connection_with_both_halves_is_usable() -> None:
    connection = AWSConnection(
        aws_access_key_id="AKIA123",
        aws_secret_access_key_encrypted="gAAAAA-not-really-a-token",
    )

    assert connection.has_credentials is True


def test_the_repr_carries_no_credential() -> None:
    """Reprs end up in logs and tracebacks. This one is id and status only, and
    adding the columns must not have changed that.
    """
    connection = AWSConnection(
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key_encrypted="gAAAAA-secret-token",
    )

    assert "AKIA" not in repr(connection)
    assert "gAAAAA" not in repr(connection)
