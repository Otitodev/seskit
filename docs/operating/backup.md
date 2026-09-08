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

!!! danger "A database backup carries secrets"
    **Webhook signing secrets**, in plaintext and deliberately: your receivers
    must read them back to verify signatures, so hashing them is not possible.

    **AWS access keys**, encrypted. The ciphertext is useless without
    `SECRET_KEY`, which is not in the database — so a dump on its own does not
    hand somebody your AWS account. A dump stored *next to* the environment
    that holds `SECRET_KEY` does.

    Either way a dump is a secret-bearing artifact. Encrypt it, restrict who
    can read it, and do not keep it beside your `.env`.

!!! warning "A backup is only restorable with the SECRET_KEY it was taken under"
    Restore a dump into an instance with a different `SECRET_KEY` and every
    stored AWS key is unreadable — each project has to be connected again.
    Whatever holds your backups should hold that secret too, and separately.

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
