"""Do the migrations build the schema the models describe?

`conftest.py` builds the test database from `Base.metadata` on purpose, so a
test fails when a model changes rather than when a migration is missing. That
is the right trade for the other twelve hundred tests and it leaves one thing
unchecked: **nothing runs the migrations against the models.**

Which means a model change with no migration behind it passes the entire suite.
Every test sees the column, because `create_all` made it; the running instance
never does, because no migration adds it. The first sign is a production
`UndefinedColumn` on a query that has passed in CI a hundred times.

So this file does what `conftest.py` does not: creates an empty database, runs
`alembic upgrade head` the way an operator does, and asks Alembic whether the
result differs from the models.

Slower than the rest of the suite, and deliberately not part of any other
fixture. It is one database, created and dropped once for this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from seskit_core.config import Settings, get_settings
from seskit_core.db import Base
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]

#: Its own database, not the one the rest of the suite shares. That one is
#: built by `create_all`, and running migrations into it would compare the
#: migrations against themselves.
MIGRATION_DATABASE = "seskit_migrations_test"

#: Alembic's own bookkeeping table. It is not in `Base.metadata` and never
#: should be, so it is the one difference that means nothing.
BOOKKEEPING = {"alembic_version"}


def _url(base: str, database: str) -> str:
    return f"{base}/{database}"


def _alembic(*args: str, url: str) -> subprocess.CompletedProcess[str]:
    """Run Alembic as a subprocess, the way the documentation says to.

    A subprocess rather than the Python API because `migrations/env.py` reads
    the URL from `get_settings()`, which is cached process-wide - driving it
    in-process would mean mutating that cache and hoping nothing else in the
    run had already read it. A child process with its own environment cannot
    reach anything here, and it exercises the command an operator actually
    types.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )
    return result


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def migrated() -> AsyncIterator[str]:
    """An empty database with `alembic upgrade head` run against it."""
    get_settings.cache_clear()
    settings: Settings = get_settings()
    base = str(settings.DATABASE_URL).rsplit("/", 1)[0]

    # CREATE/DROP has to run from another database than the one being dropped.
    admin = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DATABASE}"'))
        await connection.execute(text(f'CREATE DATABASE "{MIGRATION_DATABASE}"'))
    await admin.dispose()

    url = _url(base, MIGRATION_DATABASE)
    _alembic("upgrade", "head", url=url)

    yield url

    admin = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DATABASE}"'))
    await admin.dispose()


def _differences(connection: Connection) -> list[Any]:
    """What Alembic would autogenerate against this database.

    Empty means the migrations and the models agree. Anything in it is drift:
    each entry is a change Alembic would have written into the next migration,
    which is by definition something the migrations have not done yet.

    Configured exactly as `migrations/env.py` configures it - `compare_type`
    and `compare_server_default` are off by default, and with them off a column
    that changed from `Integer` to `String` looks identical.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    return [
        difference
        for difference in compare_metadata(context, Base.metadata)
        # A tuple whose second element is the table name, or a list of them for
        # a table-level change. Only Alembic's own version table is dropped.
        if not _is_bookkeeping(difference)
    ]


def _is_bookkeeping(difference: Any) -> bool:
    """Whether a reported difference is Alembic's own version table."""
    text_form = str(difference)
    return any(name in text_form for name in BOOKKEEPING)


# ------------------------------------------------------------------ drift ---


async def test_the_migrations_build_the_schema_the_models_describe(
    migrated: str,
) -> None:
    """The test this file exists for.

    A model changed without a migration passes every other test in the suite,
    because they all run against a database `create_all` built from the models.
    This one runs against a database the migrations built.
    """
    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(_differences)
    finally:
        await engine.dispose()

    assert not differences, (
        "the migrations and the models disagree - "
        "run `uv run alembic revision --autogenerate` and read what it writes:\n"
        + "\n".join(f"  {difference}" for difference in differences)
    )


async def test_every_table_the_models_declare_exists(migrated: str) -> None:
    """A coarser check than the one above and a much clearer failure.

    `compare_metadata` reporting a missing table prints a `Table` repr with
    every column in it. This says which table, in one line.
    """
    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as connection:
            found = set(
                await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
    finally:
        await engine.dispose()

    missing = sorted(set(Base.metadata.tables) - found)

    assert not missing, f"tables no migration creates: {missing}"


async def test_no_table_is_left_behind_by_the_models(migrated: str) -> None:
    """The other direction: a table a migration created and the models forgot.

    Usually a model deleted without its `op.drop_table`, which leaves a table
    nothing reads and nothing will ever drop.
    """
    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as connection:
            found = set(
                await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
    finally:
        await engine.dispose()

    orphans = sorted(found - set(Base.metadata.tables) - BOOKKEEPING)

    assert not orphans, f"tables the models do not declare: {orphans}"


# ------------------------------------------------------------- downgrades ---


async def test_the_whole_chain_downgrades_and_comes_back(migrated: str) -> None:
    """`docs/operating/upgrading.md` says downgrades are for development. This
    is what makes that true rather than aspirational.

    A `downgrade()` left as `pass` is invisible until somebody in development
    tries to step back one revision and cannot. Going all the way down and all
    the way up again also catches the subtler version: a downgrade that drops a
    table but leaves its enum type or index behind, so the *next* upgrade fails
    on something that already exists.

    Runs last in the file, because it leaves the database at head either way
    and the tests above should see the schema as an operator's upgrade built
    it.
    """
    _alembic("downgrade", "base", url=migrated)

    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as connection:
            remaining = set(
                await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
    finally:
        await engine.dispose()

    assert not remaining - BOOKKEEPING, (
        f"a downgrade left these behind: {sorted(remaining - BOOKKEEPING)}"
    )

    _alembic("upgrade", "head", url=migrated)


# ------------------------------------------------------------------ shape ---


def test_the_history_has_exactly_one_head() -> None:
    """Two heads mean two migrations claim the same parent, which is what a
    merge without a merge revision looks like. `alembic upgrade head` then
    fails on an instance that has one of them and not the other.
    """
    from seskit_api.doctor import _alembic_head

    assert _alembic_head() is not None


@pytest.mark.parametrize(
    "path", sorted((ROOT / "migrations" / "versions").glob("*.py")), ids=lambda p: p.name
)
def test_every_migration_says_what_it_does(path: Path) -> None:
    """A migration named by a hash and documented by nothing is one nobody can
    review. Every other module in this repository opens with why it exists.
    """
    source = path.read_text(encoding="utf-8").lstrip()

    assert source.startswith('"""'), f"{path.name} has no docstring"
