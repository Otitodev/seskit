"""`seskit reset-password` - the way back in after a forgotten password.

The login page promises that forgotten passwords are reset from the command
line. What is worth testing is the part that decides things: that the new
password works and the old one does not, that nobody is left signed in on a
session opened under the old one, that a short password changes nothing, and
that an unknown address is reported rather than invented.

The prompts are checked without a database; the reset itself needs one.
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from seskit_api import cli
from seskit_core.security.sessions import create_session, read_session
from seskit_core.services import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    authenticate,
    register_user,
)
from sqlalchemy.ext.asyncio import AsyncSession

OLD = "the-old-password-1"
NEW = "a-brand-new-password-2"


async def _user(session: AsyncSession, email: str = "owner@example.com"):  # type: ignore[no-untyped-def]
    return await register_user(session, email=email, password=OLD, allow_signup=True)


# ------------------------------------------------------------- the reset ---


async def test_the_new_password_works_and_the_old_one_does_not(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await _user(db_session)

    result = await cli.reset_account_password(
        db_session, redis_client, email="owner@example.com", password=NEW
    )

    assert result is not None
    assert await authenticate(db_session, email="owner@example.com", password=NEW) is not None
    assert await authenticate(db_session, email="owner@example.com", password=OLD) is None


async def test_the_address_is_matched_the_way_login_matches_it(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Capitalisation and stray spaces are what a person types at a prompt."""
    await _user(db_session)

    result = await cli.reset_account_password(
        db_session, redis_client, email="  Owner@Example.COM ", password=NEW
    )

    assert result is not None


async def test_sessions_opened_under_the_old_password_are_ended(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _user(db_session)
    first, _ = await create_session(redis_client, user.id, 3600)
    second, _ = await create_session(redis_client, user.id, 3600)

    result = await cli.reset_account_password(
        db_session, redis_client, email=user.email, password=NEW
    )

    assert result is not None
    assert result.sessions_ended == 2
    assert await read_session(redis_client, first, 3600) is None
    assert await read_session(redis_client, second, 3600) is None


async def test_another_accounts_sessions_are_left_alone(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    target = await _user(db_session)
    other = await _user(db_session, "other@example.com")
    token, _ = await create_session(redis_client, other.id, 3600)

    await cli.reset_account_password(db_session, redis_client, email=target.email, password=NEW)

    assert await read_session(redis_client, token, 3600) is not None


async def test_an_unknown_address_is_reported_not_created(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    result = await cli.reset_account_password(
        db_session, redis_client, email="nobody@example.com", password=NEW
    )

    assert result is None


async def test_a_short_password_changes_nothing(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _user(db_session)
    token, _ = await create_session(redis_client, user.id, 3600)

    with pytest.raises(PasswordTooShort):
        await cli.reset_account_password(
            db_session, redis_client, email=user.email, password="x" * (MIN_PASSWORD_LENGTH - 1)
        )

    assert await authenticate(db_session, email=user.email, password=OLD) is not None
    assert await read_session(redis_client, token, 3600) is not None


async def test_a_deactivated_account_is_reset_but_not_reactivated(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _user(db_session)
    user.is_active = False
    await db_session.flush()

    result = await cli.reset_account_password(
        db_session, redis_client, email=user.email, password=NEW
    )

    assert result is not None
    assert result.is_active is False
    assert await authenticate(db_session, email=user.email, password=NEW) is None


# ------------------------------------------------------------ the prompts ---


def test_two_different_passwords_change_nothing_and_never_reach_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["first-password-here", "a-different-one"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))

    code = cli.main(["reset-password", "--email", "owner@example.com"])

    assert code == cli.EXIT_USAGE
    assert "differ" in capsys.readouterr().err


def test_the_password_is_never_an_argument() -> None:
    """On the command line it would be in shell history and `ps`."""
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["reset-password", "--password", "hunter2hunter2"])


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
