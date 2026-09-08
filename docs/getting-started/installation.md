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

## On a server

```bash
curl -fsSL https://otitodev.github.io/seskit/install.sh | sh
```

Clones the repository, writes a `.env` with a generated `SECRET_KEY`, and
brings the stack up. It refuses rather than guesses: where Docker is missing or
not running it says so and stops, instead of installing things on your machine.

Safe to run twice — an existing checkout is left alone and an existing `.env`
is never overwritten, so a rerun cannot rotate `SECRET_KEY` and lock every
project out of its stored AWS credentials.

It installs the **latest published release**, not the tip of `main`, so a
commit pushed five minutes ago cannot become your production instance. Pin it
yourself, or take `main` deliberately:

```bash
curl -fsSL https://otitodev.github.io/seskit/install.sh | SESKIT_VERSION=v0.1.0 sh
curl -fsSL https://otitodev.github.io/seskit/install.sh | SESKIT_VERSION=main sh
```

| Variable | Default |
|---|---|
| `SESKIT_VERSION` | The latest release, or `main` if none is published |
| `SESKIT_DIR` | `./seskit` |
| `SESKIT_PUBLIC_URL` | Guessed from this machine's public IP |

`SESKIT_PUBLIC_URL` becomes `PUBLIC_BASE_URL`, which is what puts a working
one-click unsubscribe link on every message. The guess is right often enough to
be worth making and wrong in ways you can see — behind a proxy or on a real
domain, change the one line in `.env`.

If piping a script to a shell is not something you do — reasonable — the
three commands above are the whole of it, and
[read the script first](https://otitodev.github.io/seskit/install.sh).

Or [hand the whole thing to a coding agent](../deploy-with-an-agent.md).

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

uv run alembic upgrade head                # Compose does this for you
uv run uvicorn seskit_api.main:app --reload
uv run arq seskit_worker.main.WorkerSettings   # in a second shell
```

**Two processes, one codebase.** The API serves `/v1` and the dashboard; the
worker sends the mail, delivers webhooks and polls for delivery events. They
never talk to each other — the API records the message and puts a job in Redis,
and the worker picks it up. That is why a send answers `queued` rather than
`sent`.

The worker is not optional. Sending is queued, so with no worker running a
message stays at `queued` for ever and nothing tells you why.

Both need the *same* `DATABASE_URL`, `REDIS_URL` and `SECRET_KEY` — which is
why they are exported once above and inherited by both shells. See
[how the two processes fit together](../operating/deploying.md#how-the-two-processes-fit-together)
before running this anywhere real.

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
