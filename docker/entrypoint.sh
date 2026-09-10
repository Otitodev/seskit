#!/bin/sh
# Container entrypoint for both the API and the worker.
#
# Applies migrations before starting, but only when asked to:
#
#   MIGRATE_ON_START=true
#
# Off by default. Under Compose the one-shot `migrate` service already does
# this and the two processes wait on it, which is the better arrangement when
# it is available: it is visible in `docker compose ps`, and a failure stops
# the stack instead of being buried in an application log.
#
# It exists for platforms that offer no such thing. Render, Fly, Railway,
# Aeroplane and the rest run the image directly, and some have no release hook,
# no one-off job, and no shell - so there is nowhere to run `alembic upgrade
# head` at all, and the instance starts and then fails on its first query.
#
# Concurrency is handled in migrations/env.py, which takes a Postgres advisory
# lock around the upgrade. Two replicas starting together is the normal case on
# those platforms: one migrates, the other waits and finds nothing to do.
#
# POSIX sh, matching the base image's /bin/sh.

set -eu

# Anything but a clear yes is a no. An unset variable, an empty one - which is
# how most platforms represent "declared, not set" - and a typo all leave the
# behaviour as it was rather than quietly enabling schema changes.
case "${MIGRATE_ON_START:-}" in
    true | True | TRUE | 1 | yes | YES)
        echo "entrypoint: applying migrations (MIGRATE_ON_START is set)"
        # No `|| true`. A failed migration means the schema is not what this
        # code expects, and starting anyway turns a loud failure into a stream
        # of confusing query errors.
        alembic upgrade head
        echo "entrypoint: migrations applied"
        ;;
esac

# exec, so the application becomes PID 1 and receives the signals the platform
# sends it. Without it this shell would hold PID 1 and a stop would go to the
# shell rather than to uvicorn or arq, costing a graceful shutdown and ending
# in a kill after the timeout.
exec "$@"
