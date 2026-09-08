# Deploying

SESKit is one Python process plus a worker, PostgreSQL and Redis. There is no
separate frontend service, no `node_modules` and no JavaScript build step — the
dashboard is server-rendered by the same process that serves the API.

## What has to run

| | | |
|---|---|---|
| **API** | `uvicorn seskit_api.main:app` | Serves `/v1` and the dashboard |
| **Worker** | `arq seskit_worker.main.WorkerSettings` | Sends mail, delivers webhooks, polls SQS |
| **PostgreSQL** | 16 or later | |
| **Redis** | 7 or later | Cache, rate limits, job queue |

!!! warning "The worker is not optional"
    Sending is queued. With no worker running, messages stay at `queued` for
    ever and nothing on the dashboard explains why.

## How the two processes fit together

**They never talk to each other.** There is no RPC between them, no port one
opens for the other, and no service discovery. Everything passes through
PostgreSQL and Redis:

```text
   your application
         │ POST /v1/emails
         ▼
  ┌──────────────────┐   records the message   ┌────────────┐
  │       API        │ ──────────────────────► │ PostgreSQL │
  │     uvicorn      │                         └─────┬──────┘
  │ /v1 + dashboard  │                               │ reads it back
  └────────┬─────────┘                               │
           │ enqueues a job                          │
           ▼                                         │
      ┌─────────┐      takes the job        ┌────────┴───────┐
      │  Redis  │ ────────────────────────► │     Worker     │ ──► SES
      └─────────┘                           │      arq       │     or SMTP
                                            └────────────────┘
```

The API's work ends the moment the message is recorded and a job is queued.
That is why a send answers `queued` rather than `sent` — see
[your first email](../getting-started/first-email.md).

The worker owns everything that talks to something remote and slow: sending,
webhook delivery, polling SQS for delivery events, and re-checking identity
verification.

Four things follow from the split, and they are the ones that catch people:

- **The worker needs no inbound port.** Nothing connects *to* it. It only makes
  outbound connections, so it can sit in a private subnet with no load
  balancer, no hostname and no certificate.
- **Either can be scaled independently.** Both are stateless; the queue and the
  database hold everything. Run three workers behind one API if sending is your
  bottleneck.
- **A worker outage is invisible from the API.** Sends keep returning `201` and
  pile up at `queued`, because from the API's side nothing is wrong. That is
  what [`doctor.py`](troubleshooting.md#start-here) checks and what the
  [troubleshooting page](troubleshooting.md) opens with.
- **They must share the same Postgres, the same Redis and the same
  `SECRET_KEY`.** See [Environment](#environment) below — a mismatched secret
  fails silently rather than loudly.

## Building the images

Two Dockerfiles, identical up to the last line:

| | Starts | For |
|---|---|---|
| `docker/Dockerfile` | `uvicorn seskit_api.main:app` | The API and dashboard |
| `docker/Dockerfile.worker` | `arq seskit_worker.main.WorkerSettings` | The worker |

**Compose only uses the first.** It builds one image and overrides `command:`
for the worker service, which is the normal way to run one image two ways.

`Dockerfile.worker` exists for platforms that build from a Dockerfile and offer
no per-deployment command override — several treat the Dockerfile as the sole
source of truth for how the image starts, so there is nowhere to put the
`arq` command. On those, deploy the worker as a second service from the same
repository, pointed at `docker/Dockerfile.worker`.

!!! warning "Keep the two in step"
    They differ only in their final `CMD`. A change to the build stages of one
    belongs in the other, and nothing enforces that but review.

Both build the whole workspace, so one repository produces both services and
they cannot drift to different versions of the code.

## The smallest real deployment

The shipped `docker-compose.yml` is a development stack, not a production one — it
runs Mailpit, mounts your source for live reload, and publishes database ports
to the host. For a server, take it as a starting point and:

- Remove the Mailpit service and the source bind mounts.
- Stop publishing Postgres and Redis to the host; the Compose network is
  enough.
- Set a real `SECRET_KEY`. It refuses to boot on the example value.
- Put a TLS terminator in front — Caddy, nginx, or a load balancer.

## Environment

The required minimum, **for both processes**:

```bash
SECRET_KEY=...                     # long and random
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
```

Give them the same values. The worker is not a lesser process with a smaller
configuration — it reads the same settings from the same file, and two of them
have to match exactly.

!!! danger "A mismatched `SECRET_KEY` fails silently"
    The worker signs one-click unsubscribe links with it; the API verifies them
    with it. Give the two processes different secrets and every unsubscribe
    link in every message answers "this link is not valid" — while sending,
    delivery, webhooks and the dashboard all keep working perfectly. Nothing
    logs an error, because from each side the other's token is simply a forgery.

    Generate one, put it in a shared secret store, and give both processes the
    same reference:

    ```bash
    python -c 'import secrets; print(secrets.token_urlsafe(32))'
    ```

### `PUBLIC_BASE_URL` is read by both, for different reasons

| Process | Uses it for | Unset means |
|---|---|---|
| **Worker** | Building the `List-Unsubscribe` link on every message | The headers are left off the message entirely |
| **API** | The endpoint SNS is subscribed to, for `https` [event ingestion](../guides/delivery-events.md) | HTTPS ingestion cannot be set up; SQS polling is unaffected |

Setting it on the API alone is the easy mistake, and it is invisible: the
dashboard looks configured, and mail simply goes out with no Unsubscribe
button. `doctor.py` reports what each process sees.

**Nothing here is about AWS.** An access key belongs to a project and is
entered on the dashboard once the instance is up — there is no AWS variable to
set on the server and no CLI to install on it. See
[get an access key](../guides/get-an-access-key.md).

The one thing the deployment owes AWS credentials is `SECRET_KEY`: it derives
the key they are encrypted with, so both processes need the same value and
changing it disconnects every project.

## Migrations

**Under Compose they run themselves.** A one-shot `migrate` service applies the
schema and exits, and the API and worker wait for it to succeed — so
`docker compose up` on a clean machine produces a working instance rather than
one that starts and then fails on its first query.

A one-shot service rather than something in the API's startup, because two API
replicas booting together would race, and migrations are not a thing to run on
every boot. It is visible in `docker compose ps`, and a failure stops the stack
instead of being buried in an application log.

Running them by hand, if you deploy some other way:

```bash
docker compose run --rm migrate      # with the shipped image
uv run alembic upgrade head          # from a checkout, with the dev group
```

See [upgrading](upgrading.md) for the ordering that matters.

## Health

There are two probes and they answer different questions. Pointing the wrong
one at your orchestrator is the mistake worth avoiding, because it fails in the
direction that sends traffic to an instance that cannot serve it.

| Probe | Checks | Point your… |
|---|---|---|
| `GET /healthz` | Nothing. Returns 200 whenever the process is running. | **liveness** check at it |
| `GET /readyz` | Round-trips Postgres and Redis. Returns **503** if either is unreachable, with a `dependencies` object saying which. | **readiness** check at it |

`/healthz` answers "is this process alive, or should it be restarted?" It
deliberately touches no dependencies: a liveness probe that fails when the
database is briefly unavailable restarts a perfectly healthy process and makes
an outage worse.

`/readyz` answers "can this instance serve a request?" Use it for readiness and
for load-balancer health, so an instance that has lost its database is taken
out of rotation instead of being sent work it cannot do.

Give the container a start period — the first boot runs migrations and is
slower than the rest.

## Reachability

SESKit needs **outbound** connections to AWS and to your webhook endpoints. It
needs **inbound** connections only from your own applications and your browser.

The exception is `EVENT_INGESTION=https`, which asks SNS to POST to you and so
requires a public hostname with a valid certificate. The default `sqs` mode
polls instead and needs no inbound access at all — which is why it is the
default.
