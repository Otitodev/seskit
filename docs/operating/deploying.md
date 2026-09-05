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

## The smallest real deployment

The shipped `compose.yaml` is a development stack, not a production one — it
runs Mailpit, mounts your source for live reload, and publishes database ports
to the host. For a server, take it as a starting point and:

- Remove the Mailpit service and the source bind mounts.
- Stop publishing Postgres and Redis to the host; the Compose network is
  enough.
- Set a real `SECRET_KEY`. It refuses to boot on the example value.
- Put a TLS terminator in front — Caddy, nginx, or a load balancer.

## Environment

The required minimum:

```bash
SECRET_KEY=...                     # long and random
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
```

For AWS, give the process credentials the boto3 way. An **IAM role** on the
instance or task is better than any key, because there is no secret to leak or
rotate. See [connect an AWS account](../guides/connect-aws.md) and
[Configuration](../reference/configuration.md).

## Migrations

Run before starting the new version:

```bash
uv run alembic upgrade head
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
