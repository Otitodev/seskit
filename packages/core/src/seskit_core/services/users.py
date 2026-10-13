"""User registration and authentication.

Domain logic lives here rather than in route handlers, so it can be tested
without HTTP and reused from the CLI (§24) later (§32.12).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import DEFAULT_PROJECT_NAME, Project, User, normalise_email
from seskit_core.security.passwords import (
    burn_dummy_hash,
    hash_password,
    verify_and_update,
    verify_password,
)
from seskit_core.security.recovery import code_matches, generate_code, hash_code, key_matches

#: Long enough to be worth hashing. OWASP's floor is 8; this is an email
#: platform holding AWS access, so it asks for a little more. Here rather than in
#: a route because the sign-up form and the command line must agree on it - a
#: password the dashboard would refuse is not one a reset should accept.
MIN_PASSWORD_LENGTH = 12


class PasswordTooShort(Exception):
    """A password below :data:`MIN_PASSWORD_LENGTH`."""


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


async def reset_password(session: AsyncSession, *, email: str, password: str) -> User | None:
    """Set a new password for an account, or return None when there is none.

    For the operator, not the account holder: there is no emailed reset link,
    because a self-hosted instance may be unable to send email until somebody
    can log in and connect AWS - the situation a forgotten password is most
    likely to arrive in. Whoever can run this already holds the database and
    ``SECRET_KEY``, so naming an unknown address here discloses nothing.

    Flushes and does not commit; the caller owns the transaction. It also does
    not end sessions, which live in Redis - see
    :func:`seskit_core.security.sessions.delete_user_sessions`.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort

    user = await get_user_by_email(session, email)
    if user is None:
        return None

    user.password_hash = hash_password(password)
    await session.flush()
    return user


# --------------------------------------------------------------- recovery ---


@dataclass(frozen=True, slots=True)
class Recovered:
    """A successful recovery: who, and the replacement code to show them once."""

    user: User
    recovery_code: str


def issue_recovery_code(user: User) -> str:
    """Give an account a new recovery code, replacing any it had.

    Returns the code, which is the only moment it exists in the clear; the row
    keeps its hash. The caller flushes or commits.
    """
    code = generate_code()
    user.recovery_code_hash = hash_code(code)
    return code


async def recover_password(
    session: AsyncSession,
    *,
    email: str,
    proof: str,
    new_password: str,
    recovery_key: str = "",
) -> Recovered | None:
    """Set a new password for someone who can prove they own the account.

    ``proof`` is either the account's recovery code or the operator's recovery
    key, accepted from the same field so the page asks for one thing. Returns
    None for every way of being wrong - no such account, a deactivated one, no
    code on file, a wrong code, a wrong key - so a caller cannot tell them
    apart and the page cannot be used to find out which addresses have accounts.

    Spending a code issues the next one, in the same step: an account must not
    be left without a way back in by having used it. The operator's key is not
    spent, because it is not the account's.

    Raises :class:`PasswordTooShort` before looking at anything else, which
    reveals nothing: it depends only on what the caller typed.

    Flushes and does not commit. Sessions live in Redis and are the caller's to
    end.
    """
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort

    user = await get_user_by_email(session, email)

    # Both checks always run, whatever the first says, and run against a stand-in
    # when there is no account: the cost of a wrong answer must not depend on
    # which half was wrong.
    code_ok = code_matches(proof, user.recovery_code_hash if user else None)
    key_ok = key_matches(proof, recovery_key)

    if user is None or not user.is_active or not (code_ok or key_ok):
        return None

    user.password_hash = hash_password(new_password)
    next_code = issue_recovery_code(user)
    await session.flush()
    return Recovered(user=user, recovery_code=next_code)


async def regenerate_recovery_code(
    session: AsyncSession, user: User, *, password: str
) -> str | None:
    """Replace an account's recovery code, after the current password is given.

    The password is asked for because a signed-in session is not proof of
    ownership - it is also what a stolen cookie looks like, and a new code would
    let the thief take the account for good. None when the password is wrong.
    """
    if not verify_password(password, user.password_hash):
        return None
    code = issue_recovery_code(user)
    await session.flush()
    return code
