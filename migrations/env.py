"""Alembic environment.

The database URL comes from SESKit's own settings rather than alembic.ini, so
there is exactly one place a connection string is configured (§32.6). Alembic
has no async API of its own; the async template drives an async engine through
``run_sync``.
"""

import asyncio
from logging.config import fileConfig

# seskit_core.models is imported for its side effect: it registers every model
# on Base.metadata. Without it, autogenerate silently produces empty migrations.
import seskit_core.models  # noqa: F401
from alembic import context
from seskit_core.config import get_settings
from seskit_core.db import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(get_settings().DATABASE_URL))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - used to review a migration."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


#: Identifies SESKit's migration lock among any other advisory locks in the
#: database. Arbitrary, and only has to be stable: two runners agree because
#: they are running this same file.
MIGRATION_LOCK_ID = 8_534_127_001


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch column type and default drift, which Alembic ignores by default.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        # One migration at a time, per database.
        #
        # Alembic does not serialise concurrent runs, and nothing here used to
        # need it: the Compose `migrate` service is a single container the API
        # and worker wait on. Migrations can also be started by the container
        # entrypoint now, and a platform that starts two replicas together
        # would run two upgrades at once against one database.
        #
        # Inside the transaction, and the transaction-scoped variant of the
        # lock, both deliberately.
        #
        # Taking it before `begin_transaction` does not work, and fails in the
        # worst way available: the statement opens a transaction of its own,
        # Alembic then nests inside that rather than owning it, and the commit
        # at the end of this block commits nothing. The connection closes, the
        # outer transaction rolls back, and every migration is silently undone.
        # It reports success and leaves an empty database.
        #
        # `pg_advisory_xact_lock` also needs no unlock: it is released when
        # this transaction ends, whether it commits, rolls back, or the process
        # dies holding it. It blocks rather than failing, so a second runner
        # waits and then finds nothing left to apply - better than giving up
        # and starting against a half-migrated schema.
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})")

        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
