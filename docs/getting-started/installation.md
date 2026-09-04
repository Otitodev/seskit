# Install and run

This page is about running **the server** — the SESKit instance itself. If you
are looking for how to send mail from your application once it is running, that
is [Sending from your app](sending-from-your-app.md).

Requires [Docker](https://docs.docker.com/get-docker/). **No AWS account
needed** — local mail is captured by [Mailpit](https://mailpit.axllent.org/)
instead of being sent through SES.

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
docker compose up
```

| | |
|---|---|
| Dashboard | <http://localhost:8000> |
| API docs | <http://localhost:8000/docs> |
| Mailpit inbox | <http://localhost:8025> |

Then [send your first email](first-email.md).

## Why Postgres and Redis are on unusual ports

!!! tip "55432 and 56379, not 5432 and 6379"
    Machines with PostgreSQL installed frequently already have clusters on 5432
    *and* 5433. Those bind before Docker does, and the container then looks
    perfectly healthy while every connection quietly reaches the wrong
    database — which is a genuinely unpleasant afternoon.

    Override with `POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` if the high ports
    clash with something of yours.

Inside the Compose network these are still plain `db:5432` and `redis:6379`, so
nothing else changes.

## Running without Docker

Useful if you are working on SESKit itself, or already run Postgres and Redis.

```bash
uv sync
docker compose up -d db redis mailpit      # dependencies only

export DATABASE_URL="postgresql+asyncpg://seskit:seskit@localhost:55432/seskit"
export REDIS_URL="redis://localhost:56379/0"
export SECRET_KEY="dev"

uv run alembic upgrade head
uv run uvicorn seskit_api.main:app --reload
uv run arq seskit_worker.main.WorkerSettings   # in a second shell
```

The worker is not optional. Sending is queued, so with no worker running a
message stays at `queued` for ever and nothing tells you why.

## Creating the owner account

Open <http://localhost:8000> and register. **The first registration claims the
instance and signup closes behind you** — an instance you deploy is yours, not
an open sign-up page someone else can find.

Set `ALLOW_SIGNUP=true` if you want it to stay open.

## What next

- [Your first email](first-email.md) — about a minute, still no AWS account.
- [Connect an AWS account](../guides/connect-aws.md) — when you want real mail
  to leave the building.
- [Deploying](../operating/deploying.md) — running this somewhere other than
  your laptop.
