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
    # One migration at a time, per database.
    #
    # Alembic does not serialise concurrent runs by itself, and nothing here
    # used to need it: the Compose `migrate` service is a single container that
    # the API and worker wait on. But migrations can also be started by the
    # container entrypoint when MIGRATE_ON_START is set, and a platform that
    # starts two replicas together would then run two upgrades at once against
    # one database.
    #
    # `pg_advisory_lock` blocks rather than failing, so the second runner waits
    # and then finds there is nothing left to apply - which is the behaviour
    # worth having, since the alternative is a replica that gives up and starts
    # against a half-migrated schema. The lock is held on this connection and
    # released when it closes, including if the process is killed.
    connection.exec_driver_sql(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})")

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch column type and default drift, which Alembic ignores by default.
        compare_type=True,
        compare_server_default=True,
    )

    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})")


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
