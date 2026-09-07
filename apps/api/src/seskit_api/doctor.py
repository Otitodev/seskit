"""Check a local setup and say what is wrong (§31 Phase 13).

    uv run python scripts/doctor.py
    docker compose exec api python -m seskit_api.doctor

**The worst first hour with a self-hosted product is "it starts, and nothing
works".** Everything is up, the dashboard renders, and a send fails with a
message about configuration the reader has not met yet. This exists to turn
that into one line naming the thing to change.

**Deliberately not `/readyz`.** That probe answers "should traffic come here",
which is a different question. An instance with no SMTP and no AWS connection
is ready and correct and cannot send a single message; a readiness probe that
said otherwise would take a healthy instance out of rotation.

The checks run in the order a first hour actually fails, and each stops being
interesting once the one above it is wrong - so a failure prints and the run
continues, rather than every later check reporting the same root cause in its
own words.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from seskit_core.config import INSECURE_PLACEHOLDER, Settings, get_settings
from seskit_core.models import Email, EmailStatus
from seskit_core.models.base import utcnow
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[4]

#: How long a message may sit in `queued` before it is evidence of a worker
#: that is not running. Generous: a send takes under a second, so anything
#: still queued after this was not picked up rather than slow.
STALE_AFTER = timedelta(minutes=5)

#: The Redis index the test suite uses. Sharing it means a test run flushes
#: development sessions out from under whoever is signed in.
TEST_REDIS_DB = 15


@dataclass(frozen=True, slots=True)
class Result:
    """One check. `fix` is the single thing to change, when there is one."""

    name: str
    ok: bool
    detail: str
    fix: str = ""

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        line = f"[{mark}] {self.name}: {self.detail}"
        return line if self.ok or not self.fix else f"{line}\n         -> {self.fix}"


def check_configuration(settings: Settings) -> list[Result]:
    """The mistakes that are made before anything is started."""
    results = [
        Result(
            "secret key",
            settings.SECRET_KEY != INSECURE_PLACEHOLDER,
            "set" if settings.SECRET_KEY != INSECURE_PLACEHOLDER else "still the placeholder",
            fix="Generate one: python -c 'import secrets; print(secrets.token_urlsafe(32))'",
        ),
        Result(
            "database url",
            str(settings.DATABASE_URL).startswith("postgresql+asyncpg://"),
            "asyncpg"
            if str(settings.DATABASE_URL).startswith("postgresql+asyncpg://")
            else "not an asyncpg URL",
            fix="DATABASE_URL must start with postgresql+asyncpg://",
        ),
    ]

    # Not a failure. An instance that never sends a one-click unsubscribe
    # header and ingests events over SQS needs no public URL at all, so this
    # reports rather than refuses.
    if settings.PUBLIC_BASE_URL:
        results.append(Result("public url", True, settings.PUBLIC_BASE_URL))
    else:
        results.append(
            Result(
                "public url",
                True,
                "unset - no unsubscribe headers on outgoing mail"
                + (", and https event ingestion cannot work" if settings.receives_https else ""),
                fix="Set PUBLIC_BASE_URL to where this instance is reachable, if you want either.",
            )
        )
    return results


async def check_postgres(settings: Settings) -> list[Result]:
    """That it answers, and that the schema is the one this code expects."""
    engine = create_async_engine(str(settings.DATABASE_URL), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            applied = await connection.scalar(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
    except Exception as exc:
        return [
            Result(
                "postgres",
                False,
                _why(exc),
                fix="Start it (docker compose up -d db) and check DATABASE_URL.",
            )
        ]
    finally:
        await engine.dispose()

    results = [Result("postgres", True, "connected")]

    expected = _alembic_head()
    if expected is None:
        results.append(Result("migrations", True, f"at {applied}, head not readable here"))
    elif applied == expected:
        results.append(Result("migrations", True, f"at head ({applied})"))
    else:
        results.append(
            Result(
                "migrations",
                False,
                f"at {applied or 'nothing'}, head is {expected}",
                fix="Run: uv run alembic upgrade head",
            )
        )
    return results


#: `revision = "abc"` / `down_revision = "abc"` at the top of a migration.
_REVISION = re.compile(
    r"^(revision|down_revision)(?::[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)


def _alembic_head(root: Path = ROOT) -> str | None:
    """The newest revision in `migrations/versions`, or None if unreadable.

    Read from the files rather than through Alembic, which is a development
    dependency and is **not installed in the runtime image** - which is exactly
    where an operator most needs to be told their schema is behind.

    The head is the revision nothing else names as its `down_revision`. That is
    the definition, not an approximation, and it needs no import: parsing two
    assignments out of each file is less machinery than the library, and works
    wherever the files do.

    None rather than a failure when the directory is not there. A check that
    cannot answer should say so, not turn its own missing input into somebody
    else's bug report.
    """
    versions = root / "migrations" / "versions"
    if not versions.is_dir():
        return None

    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions.glob("*.py"):
        for kind, value in _REVISION.findall(path.read_text(encoding="utf-8")):
            (revisions if kind == "revision" else parents).add(value)

    heads = revisions - parents
    # Exactly one, or the history has branched and "are we at head" is not a
    # question with one answer - which is a thing to say rather than to guess at.
    return heads.pop() if len(heads) == 1 else None


async def check_redis(settings: Settings) -> list[Result]:
    """That it answers, and that it is not the index the tests flush."""
    from redis.asyncio import Redis

    client: Any = Redis.from_url(str(settings.REDIS_URL))
    try:
        await client.ping()
    except Exception as exc:
        return [
            Result(
                "redis",
                False,
                _why(exc),
                fix="Start it (docker compose up -d redis) and check REDIS_URL.",
            )
        ]
    finally:
        await client.aclose()

    index = str(settings.REDIS_URL).rstrip("/").rpartition("/")[2]
    if index == str(TEST_REDIS_DB):
        return [
            Result(
                "redis",
                False,
                f"connected, but on database {TEST_REDIS_DB}",
                fix=(
                    f"The test suite flushes database {TEST_REDIS_DB}. Point REDIS_URL "
                    "at another index or every test run will sign you out."
                ),
            )
        ]
    return [Result("redis", True, "connected")]


async def check_sending(settings: Settings) -> list[Result]:
    """Which path a send would actually take today.

    The question somebody asks after their first message goes nowhere, and the
    one nothing else in the product answers directly.
    """
    from seskit_core.models import AWSConnection, ConnectionStatus

    engine = create_async_engine(str(settings.DATABASE_URL))
    try:
        async with engine.connect() as connection:
            connected = await connection.scalar(
                select(func.count())
                .select_from(AWSConnection)
                .where(AWSConnection.status == ConnectionStatus.CONNECTED.value)
            )
    except Exception:
        connected = None
    finally:
        await engine.dispose()

    if connected:
        return [
            Result(
                "sending",
                True,
                f"{connected} project(s) connected to AWS - sends go to SES, "
                "if the sender is verified",
            )
        ]
    if settings.smtp_configured:
        return [
            Result(
                "sending",
                True,
                f"no AWS connection - sends go to SMTP at {settings.SMTP_HOST}",
            )
        ]
    return [
        Result(
            "sending",
            False,
            "no AWS connection and no SMTP host - every send will be refused",
            fix="Connect AWS on the dashboard, or set SMTP_HOST (Mailpit, locally).",
        )
    ]


async def check_worker(settings: Settings) -> list[Result]:
    """Whether anything is draining the queue.

    Asked as a symptom rather than as a heartbeat, because the symptom is what
    somebody notices: messages accepted, and never sent. A heartbeat would also
    have to be written, and a check that needs new machinery to answer is a
    check that can be wrong about itself.
    """
    engine = create_async_engine(str(settings.DATABASE_URL))
    try:
        async with engine.connect() as connection:
            stale = await connection.scalar(
                select(func.count())
                .select_from(Email)
                .where(
                    Email.status == EmailStatus.QUEUED.value,
                    Email.created_at < utcnow() - STALE_AFTER,
                )
            )
    except Exception:
        return [Result("worker", True, "not checked - no database")]
    finally:
        await engine.dispose()

    if not stale:
        return [Result("worker", True, "nothing stuck in the queue")]
    return [
        Result(
            "worker",
            False,
            f"{stale} message(s) queued for over {int(STALE_AFTER.total_seconds() // 60)} minutes",
            fix="Start the worker: docker compose up -d worker",
        )
    ]


def _why(exc: Exception) -> str:
    """One line of an exception, because a traceback is not an instruction."""
    text_ = str(exc).strip().splitlines()
    return text_[0] if text_ else type(exc).__name__


async def run(settings: Settings | None = None) -> list[Result]:
    """Every check, in the order a first hour fails."""
    settings = settings or get_settings()

    results = check_configuration(settings)
    results += await check_postgres(settings)
    results += await check_redis(settings)
    results += await check_sending(settings)
    results += await check_worker(settings)
    return results


def report(results: list[Result]) -> str:
    lines = [result.render() for result in results]
    failed = [result for result in results if not result.ok]
    lines.append("")
    lines.append(
        "All checks passed."
        if not failed
        else f"{len(failed)} of {len(results)} checks failed. Fix the first one first."
    )
    return "\n".join(lines)


def main() -> int:
    """Exit 0 or 1, so this works in a gate as well as by hand."""
    results = asyncio.run(run())
    print(report(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
