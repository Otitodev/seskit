"""User registration and authentication.

Domain logic lives here rather than in route handlers, so it can be tested
without HTTP and reused from the CLI (§24) later (§32.12).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import DEFAULT_PROJECT_NAME, Project, User, normalise_email
from seskit_core.security.passwords import burn_dummy_hash, hash_password, verify_and_update


class SignupClosed(Exception):
    """Registration attempted while it is not permitted."""


class EmailAlreadyRegistered(Exception):
    """An account already exists for this address."""


async def count_users(session: AsyncSession) -> int:
    result = await session.scalar(select(func.count()).select_from(User))
    return int(result or 0)


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    user: User | None = await session.scalar(
        select(User).where(User.email == normalise_email(email))
    )
    return user


async def signup_allowed(session: AsyncSession, *, allow_signup: bool) -> bool:
    """Whether registration is open.

    Always open while no account exists, so whoever installs the instance can
    claim it. Closed afterwards unless explicitly re-enabled: a self-hosted
    dashboard is often reachable before anyone is watching it, and an open
    signup form on a public URL means a stranger can take it over.
    """
    if allow_signup:
        return True
    return await count_users(session) == 0


async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    allow_signup: bool = False,
) -> User:
    """Create a user, and their first project, in one transaction.

    The project is not optional. Every feature after this phase is
    project-scoped, so a user without one would land on a dashboard where
    nothing works.

    The first account registered becomes the owner of the instance.
    """
    if not await signup_allowed(session, allow_signup=allow_signup):
        raise SignupClosed

    normalised = normalise_email(email)
    if await get_user_by_email(session, normalised) is not None:
        raise EmailAlreadyRegistered

    is_first = await count_users(session) == 0

    user = User(
        email=normalised,
        password_hash=hash_password(password),
        is_owner=is_first,
    )
    session.add(user)
    await session.flush()

    session.add(Project(user_id=user.id, name=DEFAULT_PROJECT_NAME))
    await session.flush()

    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User | None:
    """Return the user when the credentials are good, otherwise None.

    One return value for every kind of failure - unknown address, wrong
    password, deactivated account - so a caller cannot tell them apart. The
    route turns it into a single generic message for the same reason.
    """
    user = await get_user_by_email(session, email)

    if user is None:
        # Spend the same CPU time as a real verification. Skipping this makes a
        # missing account answer measurably faster, which turns the login form
        # into an account-enumeration oracle.
        burn_dummy_hash()
        return None

    valid, updated_hash = verify_and_update(password, user.password_hash)
    if not valid:
        return None

    if not user.is_active:
        return None

    if updated_hash is not None:
        # The stored hash used weaker parameters than current policy. Upgrade it
        # now, while the plaintext is in hand.
        user.password_hash = updated_hash
        await session.flush()

    return user
