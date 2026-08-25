"""User and Project models, against a real database.

These run against Postgres rather than a mock because the things worth testing
here - a unique constraint, a cascade delete, a server-side default - are
enforced by the database and simply do not exist in a stub.
"""

from __future__ import annotations

import pytest
from seskit_core.ids import IDPrefix, has_prefix
from seskit_core.models import Project, User, normalise_email
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
