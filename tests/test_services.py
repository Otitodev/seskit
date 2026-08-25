"""User and project domain logic."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from seskit_core.models import Project, User
from seskit_core.security.passwords import verify_password
from seskit_core.security.throttle import clear, is_throttled, record_failure
from seskit_core.services import (
    EmailAlreadyRegistered,
    SignupClosed,
    authenticate,
    count_users,
    create_project,
    get_default_project,
    get_owned_project,
    get_user_by_email,
    list_projects,
    register_user,
    signup_allowed,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"


# ------------------------------------------------------------ registration ---


async def test_first_registration_is_always_allowed(db_session: AsyncSession) -> None:
    """A fresh install must be claimable even with signup nominally closed."""
    assert await signup_allowed(db_session, allow_signup=False) is True


async def test_signup_closes_after_the_first_user(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await signup_allowed(db_session, allow_signup=False) is False


async def test_signup_can_be_reopened_by_config(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await signup_allowed(db_session, allow_signup=True) is True


async def test_second_registration_is_refused_when_closed(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    with pytest.raises(SignupClosed):
        await register_user(db_session, email="stranger@example.com", password=PASSWORD)


async def test_first_user_becomes_the_owner(db_session: AsyncSession) -> None:
    owner = await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert owner.is_owner is True


async def test_later_users_are_not_owners(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    second = await register_user(
        db_session, email="second@example.com", password=PASSWORD, allow_signup=True
    )

    assert second.is_owner is False


async def test_registration_creates_a_default_project(db_session: AsyncSession) -> None:
    """Not optional - every later feature is project-scoped.

    A user without a project would land on a dashboard where nothing works.
    """
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)

    projects = await list_projects(db_session, user.id)
    assert len(projects) == 1
    assert projects[0].name == "Default"


async def test_password_is_hashed_not_stored(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert user.password_hash != PASSWORD
    assert verify_password(PASSWORD, user.password_hash) is True


async def test_email_is_normalised_on_registration(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="  Owner@Example.COM ", password=PASSWORD)

    assert user.email == "owner@example.com"


async def test_duplicate_email_is_refused(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    with pytest.raises(EmailAlreadyRegistered):
        await register_user(
            db_session, email="OWNER@EXAMPLE.COM", password=PASSWORD, allow_signup=True
        )


async def test_refused_registration_leaves_nothing_behind(db_session: AsyncSession) -> None:
    """A rejected signup must not leave a half-made user or an orphan project."""
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    with pytest.raises(SignupClosed):
        await register_user(db_session, email="stranger@example.com", password=PASSWORD)

    assert await count_users(db_session) == 1
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 1


# ---------------------------------------------------------- authentication ---


async def test_correct_credentials_authenticate(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    user = await authenticate(db_session, email="owner@example.com", password=PASSWORD)

    assert user is not None
    assert user.email == "owner@example.com"


async def test_login_email_is_case_insensitive(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await authenticate(db_session, email="Owner@Example.COM", password=PASSWORD) is not None


async def test_wrong_password_fails(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await authenticate(db_session, email="owner@example.com", password="wrong") is None


async def test_unknown_email_fails(db_session: AsyncSession) -> None:
    """Same None as a wrong password: the caller cannot tell them apart."""
    assert await authenticate(db_session, email="nobody@example.com", password=PASSWORD) is None


async def test_deactivated_user_cannot_authenticate(db_session: AsyncSession) -> None:
    """Deactivating is how an account is disabled without deleting its history."""
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    user.is_active = False
    await db_session.flush()

    assert await authenticate(db_session, email="owner@example.com", password=PASSWORD) is None


async def test_weak_hash_is_upgraded_on_login(db_session: AsyncSession) -> None:
    """Cost parameters can be raised without locking anyone out.

    The stored hash is replaced during the next successful login, while the
    plaintext is briefly in hand.
    """
    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher

    weak = PasswordHash((Argon2Hasher(memory_cost=8192, time_cost=1, parallelism=1),))
    user = User(email="owner@example.com", password_hash=weak.hash(PASSWORD))
    db_session.add(user)
    await db_session.flush()
    old_hash = user.password_hash

    authenticated = await authenticate(db_session, email="owner@example.com", password=PASSWORD)

    assert authenticated is not None
    assert authenticated.password_hash != old_hash
    assert verify_password(PASSWORD, authenticated.password_hash) is True


async def test_wrong_password_does_not_rehash(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    original = user.password_hash

    await authenticate(db_session, email="owner@example.com", password="wrong")

    assert user.password_hash == original


async def test_lookup_by_email_normalises(db_session: AsyncSession) -> None:
    await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await get_user_by_email(db_session, "  OWNER@example.com  ") is not None


# ---------------------------------------------------- the ownership boundary ---


async def test_owner_can_read_their_project(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    project = (await list_projects(db_session, user.id))[0]

    found = await get_owned_project(db_session, project_id=project.id, user_id=user.id)

    assert found is not None


async def test_another_user_cannot_read_it(db_session: AsyncSession) -> None:
    """The tenancy boundary. Everything after this phase depends on it holding."""
    owner = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    intruder = await register_user(
        db_session, email="intruder@example.com", password=PASSWORD, allow_signup=True
    )
    victim_project = (await list_projects(db_session, owner.id))[0]

    found = await get_owned_project(db_session, project_id=victim_project.id, user_id=intruder.id)

    assert found is None


async def test_unknown_project_reads_as_none(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)

    assert await get_owned_project(db_session, project_id="proj_nope", user_id=user.id) is None


async def test_list_projects_shows_only_your_own(db_session: AsyncSession) -> None:
    owner = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    other = await register_user(
        db_session, email="other@example.com", password=PASSWORD, allow_signup=True
    )

    assert len(await list_projects(db_session, owner.id)) == 1
    assert len(await list_projects(db_session, other.id)) == 1


async def test_projects_list_oldest_first(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    await create_project(db_session, user_id=user.id, name="Staging")

    names = [p.name for p in await list_projects(db_session, user.id)]

    assert names == ["Default", "Staging"]


async def test_default_project_is_the_oldest(db_session: AsyncSession) -> None:
    user = await register_user(db_session, email="owner@example.com", password=PASSWORD)
    await create_project(db_session, user_id=user.id, name="Staging")

    default = await get_default_project(db_session, user.id)

    assert default is not None
    assert default.name == "Default"


# --------------------------------------------------------------- throttling ---


async def test_login_is_not_throttled_initially(redis_client: Redis) -> None:
    assert await is_throttled(redis_client, "a@example.com", "1.2.3.4", 3) is False


async def test_login_throttles_after_enough_failures(redis_client: Redis) -> None:
    for _ in range(3):
        await record_failure(redis_client, "a@example.com", "1.2.3.4", 900)

    assert await is_throttled(redis_client, "a@example.com", "1.2.3.4", 3) is True


async def test_throttle_counts_up(redis_client: Redis) -> None:
    assert await record_failure(redis_client, "a@example.com", "1.2.3.4", 900) == 1
    assert await record_failure(redis_client, "a@example.com", "1.2.3.4", 900) == 2


async def test_throttle_is_per_address(redis_client: Redis) -> None:
    """Otherwise anyone could lock a known user out by failing on purpose."""
    for _ in range(3):
        await record_failure(redis_client, "a@example.com", "1.2.3.4", 900)

    assert await is_throttled(redis_client, "a@example.com", "9.9.9.9", 3) is False


async def test_throttle_is_per_email(redis_client: Redis) -> None:
    """Otherwise one attacker behind a shared NAT locks out everyone with them."""
    for _ in range(3):
        await record_failure(redis_client, "a@example.com", "1.2.3.4", 900)

    assert await is_throttled(redis_client, "b@example.com", "1.2.3.4", 3) is False


async def test_throttle_expires(redis_client: Redis) -> None:
    await record_failure(redis_client, "a@example.com", "1.2.3.4", 900)

    assert 0 < await redis_client.ttl("login_attempts:a@example.com:1.2.3.4") <= 900


async def test_successful_login_clears_the_counter(redis_client: Redis) -> None:
    """A user who mistypes then gets in must not be locked out on their next visit."""
    for _ in range(2):
        await record_failure(redis_client, "a@example.com", "1.2.3.4", 900)

    await clear(redis_client, "a@example.com", "1.2.3.4")

    assert await is_throttled(redis_client, "a@example.com", "1.2.3.4", 3) is False
