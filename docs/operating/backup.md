# Backup and restore

## What to back up

**PostgreSQL. That is the whole answer** — and it is worth knowing why the
other components are not on the list.

| | | |
|---|---|---|
| **PostgreSQL** | **Back up** | Accounts, projects, keys, messages, events, webhook endpoints and their signing secrets |
| Redis | No | Cache, rate-limit counters and the job queue. Rebuilds itself; a loss costs you in-flight sends, not data |
| Uploaded files | None exist | Attachments live in Postgres |
| AWS resources | No | The queue, topic and configuration set are recreated by pressing the button again |

!!! danger "A database backup carries the webhook signing secrets"
    They are stored in plaintext, deliberately: your receivers must read them
    back to verify signatures, so hashing them is not possible.

    That makes a dump a secret-bearing artifact. Encrypt it and restrict who
    can read it, the same way you would treat the database itself.

## Taking one

```bash
docker compose exec db pg_dump -U seskit seskit > backup.sql
```

Or against a managed database:

```bash
pg_dump "$DATABASE_URL" > backup.sql
```

Automate it, keep more than one, and keep at least one somewhere the instance
cannot reach. A backup on the same host survives everything except the things
that actually happen.

## Restoring

```bash
docker compose up -d db
docker compose exec -T db psql -U seskit -d seskit < backup.sql
uv run alembic upgrade head       # if the backup predates the running code
docker compose up -d
```

## Prove it works

An untested backup is a belief, not a backup. Restore into a scratch database
and count what came back:

```bash
psql seskit_restore_test < backup.sql
psql seskit_restore_test -c "select count(*) from emails;"
psql seskit_restore_test -c "select count(*) from webhook_endpoints;"
```

Do it on a schedule you actually keep. The failure this catches — a dump that
has been silently truncated for months — is common, and invisible until the day
it matters.

## What a restore does not bring back

- **In-flight sends.** Anything queued in Redis at the moment of failure is
  gone. Messages already recorded stay at `queued` and can be inspected.
- **Delivery events that arrived during the outage**, if you use HTTPS
  ingestion: SNS retries for a while and then stops. The SQS path is more
  forgiving, because messages wait in the queue until the worker returns.
