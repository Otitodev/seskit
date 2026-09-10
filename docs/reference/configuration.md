# Configuration

Everything comes from the environment, or a local `.env`. Copy the annotated
[`.env.example`](https://github.com/Otitodev/seskit/blob/main/.env.example) and
edit it:

```bash
cp .env.example .env
```

## The minimum to run

Three settings have no default, and SESKit will not start without them:

| Variable | |
|---|---|
| `SECRET_KEY` | Signs sessions, and derives the key stored AWS credentials are encrypted with |
| `DATABASE_URL` | PostgreSQL, with the async driver: `postgresql+asyncpg://…` |
| `REDIS_URL` | `redis://…` |

**`.env.example` already sets all three**, so `cp .env.example .env && docker
compose up` needs nothing else — those URLs point at the `db` and `redis`
containers [Compose starts for
you](../getting-started/installation.md#you-do-not-install-postgresql-or-redis).

The one to change before deploying anywhere real is `SECRET_KEY`. It ships as
`changeme` and is refused outside `local`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Everything else on this page has a working default.

!!! tip "A blank variable is the same as an unset one"
    `LOG_LEVEL=` gets you `INFO`, not an error. Most hosting platforms
    represent "declared, no value" as an empty string rather than by leaving
    the variable out, so a deploy form with rows you left empty is the normal
    case, not a broken one.

    The three above are the exception. They have nothing to fall back to, so a
    blank one refuses to boot exactly as an absent one does — defaulting a
    signing key away is worse than failing to start.

## Variables that are not SESKit settings

Five variables in `.env.example` are read by **Docker Compose**, not by SESKit.
They configure the containers, and setting them does nothing to a SESKit
running outside Compose:

| Variable | Default | |
|---|---|---|
| `POSTGRES_USER` | `seskit` | Passed to the `db` container when it first initialises |
| `POSTGRES_PASSWORD` | `seskit` | Same. Has to agree with `DATABASE_URL` |
| `POSTGRES_DB` | `seskit` | Same |
| `POSTGRES_HOST_PORT` | `55432` | Where `db` is reachable **from your machine**. Inside Compose it is always `5432` |
| `REDIS_HOST_PORT` | `56379` | Same, for Redis. Inside Compose, always `6379` |

Changing the first three after the database has been created does not rename
anything — Postgres only reads them when it initialises an empty data
directory. The host ports are high and SESKit-specific on purpose:
[why](../getting-started/installation.md#why-postgres-and-redis-are-on-unusual-ports).

## One variable is read by the container

`MIGRATE_ON_START` is read by the image's entrypoint before Python starts, so
it is not a setting either. Set it to `true` and the container applies
migrations before starting the process.

Off by default, and left off under Compose, where the one-shot `migrate`
service already does this. It is for a platform that runs the image directly
and gives you no release hook, no one-off job and no shell — see
[deploying](../operating/deploying.md#on-a-platform-with-no-compose).

`FORWARDED_ALLOW_IPS` is read by Uvicorn rather than by SESKit. Set it to `*`
behind a proxy that terminates TLS, or the session cookie loses its `Secure`
flag and redirects can point at `http://` — see
[deploying](../operating/deploying.md#behind-a-proxy-that-terminates-tls).

## There is no AWS credential setting

!!! note "They are per project, not per instance"
    An AWS access key belongs to a project and is entered on the dashboard, not
    set here — which is what lets two projects on one instance send through two
    different AWS accounts.

    The secret is stored encrypted under a key derived from `SECRET_KEY`. See
    [get an access key](../guides/get-an-access-key.md).

## Every setting

Grouped as they are in the code. Anything marked **tuning** has a default
chosen deliberately and is not worth changing without a reason — they are here
so a value you find in the code has somewhere to be looked up.

### General

| Variable | Default | |
|---|---|---|
| `PROJECT_NAME` | `SESKit` | Names the API in its own OpenAPI document |
| `ENVIRONMENT` | `local` | `local` · `staging` · `production`. Outside `local`, placeholder secrets are refused |
| `LOG_LEVEL` | `INFO` | `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `SECRET_KEY` | *required* | See [the minimum](#the-minimum-to-run) |

### Persistence

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | *required* | PostgreSQL, async driver |
| `REDIS_URL` | *required* | The job queue, the auth cache and rate limits |

### Dashboard authentication

| Variable | Default | |
|---|---|---|
| `ALLOW_SIGNUP` | `false` | Registration opens anyway while no account exists, then closes behind the first one |
| `SESSION_COOKIE_NAME` | `seskit_session` | |
| `SESSION_TTL_DAYS` | `14` | Idle timeout, not absolute — reading a session refreshes it, so an active user is never signed out mid-task |
| `LOGIN_MAX_ATTEMPTS` | `10` | Failed logins per email and IP before the door closes for a while |
| `LOGIN_ATTEMPT_WINDOW_SECONDS` | `900` | The window those attempts are counted in |

### The public API

| Variable | Default | |
|---|---|---|
| `API_RATE_LIMIT_PER_MINUTE` | `100` | **Per project, not per key** — a second key cannot buy more quota |
| `API_RATE_LIMIT_WINDOW_SECONDS` | `60` | tuning |
| `API_KEY_CACHE_TTL_SECONDS` | `60` | tuning. Only a backstop: revoking a key deletes the entry outright rather than waiting this out |
| `API_KEY_LAST_USED_INTERVAL_SECONDS` | `60` | tuning. `last_used_at` is written at most this often, so an API call does not cost a database write for a column read twice a day |
| `MAX_REQUEST_BYTES` | derived | The most one request body may be, refused before it is read. Unset means half again `EMAIL_MAX_MESSAGE_BYTES` |

### AWS

| Variable | Default | |
|---|---|---|
| `AWS_DEFAULT_REGION` | `us-east-1` | Pre-selects the region in the connect form. Not authoritative — the region in use is stored per connection |
| `AWS_STATUS_CACHE_TTL_SECONDS` | `300` | How long a rendered AWS connection stays cached. The page has an explicit Refresh for when quota or sandbox state changes |

### Delivery events

| Variable | Default | |
|---|---|---|
| `EVENT_INGESTION` | `sqs` | `sqs` polls a queue and works behind NAT · `https` has SNS post to this instance, and needs `PUBLIC_BASE_URL` and a certificate · `both` during a migration between them |
| `EVENT_RESOURCE_PREFIX` | `seskit` | Names the SQS queue and SNS topic. Changing it after setup orphans the previous resources — teardown removes what was recorded, not what this now names |
| `EVENT_CONFIGURATION_SET` | `seskit` | The SES configuration set sends go through. Without one, SES publishes no events at all |
| `PUBLIC_BASE_URL` | — | Where this instance is reachable from outside. Required for `https` ingestion, and for [one-click unsubscribe](../guides/suppression.md#one-click-unsubscribe) links — without it those headers are left off messages entirely |
| `EVENT_POLL_WAIT_SECONDS` | `20` | tuning. SQS caps long polling here |
| `EVENT_POLL_MAX_BATCHES` | `10` | tuning. Bounds one pass, so a backlog cannot starve sends |
| `EVENT_VISIBILITY_TIMEOUT_SECONDS` | `60` | tuning. Must comfortably exceed one ingest, or a slow database becomes duplicate processing |

### Webhooks

| Variable | Default | |
|---|---|---|
| `WEBHOOK_ALLOWED_CIDRS` | — | Comma-separated. The deliberate hole in the SSRF defence — see [below](#the-ones-worth-thinking-about) |
| `WEBHOOK_MAX_ATTEMPTS` | `6` | Attempts before a delivery is abandoned. With the backoff below, roughly five minutes |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | tuning. Connect and read together |
| `WEBHOOK_RETRY_BASE_SECONDS` | `5` | tuning. Doubles each attempt, with jitter |
| `WEBHOOK_FAILURE_LIMIT` | `10` | Consecutive failures before an endpoint is switched off. Any success resets it |
| `WEBHOOK_RESPONSE_CAPTURE_BYTES` | `4096` | tuning. How much of a response is kept for the delivery log |

### Identity verification

| Variable | Default | |
|---|---|---|
| `IDENTITY_RECHECK_UNVERIFIED_SECONDS` | `21600` — 6 hours | tuning. DNS takes hours, so checking sooner mostly spends quota to be told the same thing |
| `IDENTITY_RECHECK_VERIFIED_SECONDS` | `2592000` — 30 days | tuning. Not never: this is what catches a DKIM record deleted months after setup |
| `IDENTITY_REFRESH_INTERVAL_SECONDS` | `60` | tuning. Floor on forcing a re-check from the dashboard |

### Sending

| Variable | Default | |
|---|---|---|
| `EMAIL_MAX_MESSAGE_BYTES` | `10485760` — 10 MB | SES's own ceiling. Checked against the **assembled** message: base64 inflates content by about a third, so a 9 MB attachment is an over-limit message |
| `EMAIL_SEND_MAX_ATTEMPTS` | `3` | Retries for a failure worth retrying. Terminal rejections are not retried at all |

### Local email

Points at Mailpit in development. Once a project has an AWS connection and a
verified sender, its mail goes through SES instead and these stop applying to
it.

| Variable | Default | |
|---|---|---|
| `SMTP_HOST` | — | Setting it is what turns SMTP delivery on |
| `SMTP_PORT` | `1025` | |
| `SMTP_TLS` | `false` | |
| `SMTP_USER` | — | |
| `SMTP_PASSWORD` | — | |
| `EMAILS_FROM_EMAIL` | — | Fallback sender for local delivery |

## The ones worth thinking about

**`SECRET_KEY`** signs session cookies *and* derives the key that stored AWS
credentials are encrypted with. Changing it signs everyone out **and
disconnects every project from AWS** — see
[upgrading](../operating/upgrading.md). It refuses to boot on the example
value, because an instance running with a publicly known signing key is an
instance anyone can forge a session for.

**`ALLOW_SIGNUP`** is `false`, and registration opens anyway while the instance
has no accounts — so the first person to arrive claims it and the door closes
behind them. An instance you deploy is yours, not an open sign-up page someone
else can find.

**`EVENT_INGESTION`** decides how delivery events get back. `sqs` works from
anywhere including a laptop; `https` is lower latency but needs SESKit
reachable from the internet with a certificate. See
[delivery events](../guides/delivery-events.md).

**`WEBHOOK_ALLOWED_CIDRS`** is the deliberate hole in the SSRF defence. SESKit
will not POST to a loopback, private or link-local address outside local
development, because delivery responses are captured and shown in the
dashboard — which would turn a webhook into a read primitive against your
internal network. Only list ranges you actually need to reach.
