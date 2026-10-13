"""The ``seskit`` command (§24): what an operator does with a shell and no browser.

    seskit reset-password
    docker compose exec api seskit reset-password
    docker compose exec api python -m seskit_api.cli reset-password

**Why this is the first command.** A forgotten password is the one problem the
dashboard cannot solve for itself. There is no emailed reset link, on purpose: a
fresh instance may not be able to send email until somebody can log in and
connect AWS, which is exactly when a password gets forgotten.

This is for an operator with a shell. Someone without one - a deployment made by
pushing to Git has none - recovers from the Forgot password page with a recovery
code or the ``RECOVERY_KEY``; see ``seskit_core.security.recovery``.

**Why it asks rather than accepts.** The password is read from a hidden prompt
and is deliberately not an argument or an environment variable: either would put
it in shell history or a process listing. The email may be an argument, because
it is not a secret.

**Why it ends the account's sessions.** Someone resetting a password has either
lost it or lost control of it, and in the second case every session already open
is the thing being reset away. Ending them costs a legitimate user one login.

It does not reactivate a deactivated account, and says so.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from seskit_core.db import dispose_engine, get_session_factory
from seskit_core.redis import close_redis, get_redis
from seskit_core.security.sessions import delete_user_sessions
from seskit_core.services import MIN_PASSWORD_LENGTH, PasswordTooShort, reset_password
from sqlalchemy.ext.asyncio import AsyncSession

EXIT_OK = 0
EXIT_NO_ACCOUNT = 1
EXIT_USAGE = 2


@dataclass(frozen=True, slots=True)
class Reset:
    """What a reset did, for the command to report."""

    is_active: bool
    sessions_ended: int


async def reset_account_password(
    session: AsyncSession, redis: Redis, *, email: str, password: str
) -> Reset | None:
    """Set the password, then end the account's sessions. None when there is no account.

    In that order: the new password is committed before anything is revoked, so a
    Redis failure leaves the password changed and the old sessions standing -
    which is recoverable by running it again - rather than sessions revoked and
    the old password still in force.

    Raises :class:`PasswordTooShort`.
    """
    user = await reset_password(session, email=email, password=password)
    if user is None:
        return None
    await session.commit()

    ended = await delete_user_sessions(redis, user.id)
    return Reset(is_active=user.is_active, sessions_ended=ended)


def _fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


#: Shown when the address matches nothing. A sentence, not a query the program
#: runs; it assumes the Compose stack because that is how SESKit is installed.
LIST_ACCOUNTS_HINT = (
    "The addresses on this instance:  "
    'docker compose exec db psql -U seskit seskit -c "select email from users;"'
)


def _prompt(email: str | None) -> tuple[str, str] | int:
    """The address and the new password, or an exit code when they cannot be had.

    Synchronous and before the event loop starts: ``input`` and ``getpass``
    block, and nothing else is waiting on this process.
    """
    address = (email or input("Account email: ")).strip()
    if not address:
        return _fail("An email address is required.", EXIT_USAGE)

    password = getpass.getpass("New password: ")
    if getpass.getpass("Again: ") != password:
        return _fail("The two passwords differ. Nothing was changed.", EXIT_USAGE)
    return address, password


async def _reset_password_command(address: str, password: str) -> int:
    try:
        async with get_session_factory()() as session:
            result = await reset_account_password(
                session, get_redis(), email=address, password=password
            )
    except PasswordTooShort:
        return _fail(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. Nothing was changed.", EXIT_USAGE
        )
    finally:
        await dispose_engine()
        await close_redis()

    if result is None:
        return _fail(
            f"There is no account for {address}. Nothing was changed.\n{LIST_ACCOUNTS_HINT}",
            EXIT_NO_ACCOUNT,
        )

    print("Password changed.")
    if result.sessions_ended:
        print(f"Signed out {result.sessions_ended} open session(s).")
    if not result.is_active:
        print(
            "This account is deactivated, so it still cannot sign in. "
            "Resetting a password does not reactivate it."
        )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seskit", description="Operate a SESKit instance.")
    commands = parser.add_subparsers(dest="command", required=True)

    reset = commands.add_parser(
        "reset-password",
        help="set a new dashboard password for an account",
        description=(
            "Set a new dashboard password. The password is asked for, never "
            "passed as an argument, so it stays out of shell history."
        ),
    )
    reset.add_argument("--email", help="the account's email address (asked for if omitted)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "reset-password":
        answers = _prompt(args.email)
        if isinstance(answers, int):
            return answers
        return asyncio.run(_reset_password_command(*answers))
    return EXIT_USAGE  # pragma: no cover - argparse rejects an unknown command first


if __name__ == "__main__":
    raise SystemExit(main())
