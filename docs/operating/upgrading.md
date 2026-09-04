# Upgrading and migrations

## The order that matters

```bash
git pull
docker compose build
uv run alembic upgrade head    # migrations first
docker compose up -d           # then the new code
```

Migrations run **before** the new code starts. The database is written to be
one step ahead of the application, not one behind: new code against an old
schema fails immediately and loudly, where old code against a new schema
usually works right up until it corrupts something.

## Back up first

Every time, not only when it looks risky:

```bash
docker compose exec db pg_dump -U seskit seskit > backup.sql
```

A migration that fails halfway is the case this protects you from, and it is
not a case you can reason your way out of afterwards. See
[backup and restore](backup.md).

## Reading a migration before running it

```bash
uv run alembic current           # where you are
uv run alembic history           # what exists
uv run alembic upgrade head --sql > pending.sql
```

The last one prints the SQL instead of executing it. Worth reading against a
database with data you care about, particularly for anything that drops a
column or rewrites a table.

## Downgrades are not a recovery plan

Alembic can generate them. That does not make them a rollback: a downgrade that
drops a column destroys the data in it just as thoroughly as the mistake you
are trying to undo.

**Restore from the backup instead.** Downgrades are for development.

## Zero-downtime

Not currently a supported story, and worth saying plainly rather than implying
otherwise. A short maintenance window is the supported path.

If you need continuity, run the API behind a load balancer with more than one
instance — and accept that during a migration some requests meet the old schema
and some the new. That is only safe for migrations written to be
backward-compatible, and SESKit's are not audited for that yet.

## After upgrading

- `GET /healthz` returns 200.
- The worker is running. Send a test message and confirm it leaves `queued`.
- If you use [delivery events](../guides/delivery-events.md), confirm the
  worker is still consuming the queue rather than silently failing to.
