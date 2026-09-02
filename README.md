<div align="center">

# SESKit

**A Python-native developer email platform built on Amazon SES.**

Connect an AWS account, verify a sender, create an API key, send email —
and see what happened to it.

[![CI](https://github.com/Otitodev/seskit/actions/workflows/ci.yml/badge.svg)](https://github.com/Otitodev/seskit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

</div>

> **AWS should handle email infrastructure. SESKit should handle developer
> experience.**

```bash
curl -X POST https://your-seskit-instance/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "from": "hello@example.com",
    "to": ["user@example.com"],
    "subject": "Welcome",
    "html": "<h1>Hello!</h1>"
  }'
```

Self-hosted, MIT licensed, and it runs in your own AWS account.

---

## Contents

- [Why SESKit](#why-seskit)
- [What works today](#what-works-today)
- [Quickstart](#quickstart) — running in two commands, no AWS account
- [Sending for real](#sending-for-real)
  - [Connect AWS](#connect-aws) · [IAM policies](#iam-policies) ·
    [Verify a sender](#verify-a-sender) · [The SES sandbox](#the-ses-sandbox)
- [Delivery events](#delivery-events)
- [Webhooks](#webhooks) — signed events pushed to your application
- [API](#api)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Contributing](#contributing)
- [License](#license)

---

## Why SESKit

Open-source Resend-on-SES platforms already exist, and they are good — but they
are JavaScript end to end. SESKit is for teams who would rather not add a Node
toolchain to their stack to self-host their email infrastructure:

- **A FastAPI backend and a first-class Python SDK**, not a TypeScript-only
  client.
- **One service to run.** The dashboard is server-rendered by the same Python
  process that serves the API — no separate frontend service, no `node_modules`,
  no JavaScript build step.
- **Your AWS account, your costs.** SESKit is the control plane; Amazon SES does
  the delivery.

---

## What works today

SESKit is in active development. This is honest about what is built:

| | Capability | |
|---|---|---|
| ✅ | Accounts, sessions, projects | Server-rendered dashboard |
| ✅ | API keys | SHA-256 hashed, shown once, revocable |
| ✅ | AWS connection | Credentials never stored; sandbox and quota surfaced |
| ✅ | Sender verification | Email addresses and domains, with DKIM records |
| ✅ | Sending | `POST /v1/emails`, attachments, idempotency, queued delivery |
| ✅ | Delivery events | Delivered, bounced, complained — via SQS or HTTPS |
| ✅ | Customer webhooks | Signed, retried, with delivery history |
| ⬜ | Analytics dashboard | Next |
| ⬜ | Python SDK | Currently a stub — use the HTTP API |
| ⬜ | Production hardening | |

> [!NOTE]
> The Python SDK is **not implemented yet**. Its intended shape is
> `client.emails.send(...)`, but until it lands, call the HTTP API directly as
> shown throughout this README.

Full build order: [`SESKit_MVP.md`](SESKit_MVP.md) §31.

---

## Quickstart

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
| Dashboard | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Mailpit inbox | http://localhost:8025 |

> [!TIP]
> Postgres and Redis are published on **55432** and **56379** rather than their
> standard ports. Machines with PostgreSQL installed frequently already have
> clusters on 5432 *and* 5433; those bind before Docker does, and the container
> then looks healthy while every connection quietly reaches the wrong database.
> Override with `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT`.

### Send your first email

Nothing here needs an AWS account. A project with no AWS connection sends
through Mailpit, so you can watch a real message go through the whole pipeline
before deciding whether SESKit is worth configuring.

1. Open http://localhost:8000 and create the owner account. The first
   registration claims the instance; signup closes behind you.
2. Go to **API Keys**, create one, and copy it — it is shown once.
3. Send:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "from": "hello@example.com",
    "to": ["you@example.com"],
    "subject": "Hello from SESKit",
    "html": "<h1>It works</h1>"
  }'
```

```json
{ "id": "email_01J8XQ...", "status": "queued" }
```

The message appears at http://localhost:8025 within a second or two, and under
**Emails** in the dashboard with its status and provider message id.

`queued` is literal: the request is validated and recorded synchronously, then
the send itself runs in the worker. Anything you could have got wrong — an
unverified sender, a malformed address, an oversized attachment — comes back
immediately as an error rather than surfacing in a log later.

Repeat the request with an `Idempotency-Key` header and you get the same email
id back, and no second message.

### Running without Docker

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

---

## Sending for real

Sending switches from Mailpit to Amazon SES for a project once two things are
true: an AWS account is connected, and the `from` address is covered by a
verified identity. **Nothing changes in your code** — same endpoint, same
request.

> [!IMPORTANT]
> If a project has connected AWS but the sender is not verified, the send is
> **refused** rather than quietly delivered locally. Falling back would report
> success while the message reached nobody.

### Connect AWS

SESKit never stores AWS credentials. It resolves them the standard boto3 way, in
boto3's own order of precedence:

1. an IAM role attached to the EC2 instance, ECS task, or EKS pod
2. the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
3. a shared credentials file (`~/.aws/credentials`)
4. SSO or workload identity where configured

Give the process credentials by whichever suits your deployment, then open
**AWS** in the dashboard, choose your SES region, and connect. SESKit asks AWS
who the identity is and what it may do, and records the answer. **Connecting
creates nothing in your AWS account.**

Setting up [delivery events](#delivery-events) does create things — a queue, a
topic and a configuration set — but that is a separate button, it tells you what
it will create first, and disconnecting removes them again.

### IAM policies

§9 of the spec is explicit that SESKit must never ask for `AdministratorAccess`.
There are two policies because they grant genuinely different things, and you
should be able to run SESKit without the second.

<details>
<summary><b>Sending — six actions</b> (required)</summary>

Everything except delivery events. All of it is scoped to SES, and only one
action can create anything.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "ses:GetAccount",
        "ses:CreateEmailIdentity",
        "ses:GetEmailIdentity",
        "ses:DeleteEmailIdentity",
        "ses:SendEmail"
      ],
      "Resource": "*"
    }
  ]
}
```

The first two are read-only and are all that connecting an account needs, so you
can grant just those to look around before committing to anything. The identity
actions create and remove the verified senders on the Domains page.

`ses:SendEmail` is the one to grant deliberately: it is the only action here that
can reach the outside world and appear on your AWS bill. You do not need it to
try SESKit — without an AWS connection, sending goes to Mailpit locally.

Removing an identity is the only destructive thing here, and it is guarded:
SESKit deletes the identity in SES only when no other project is still using it.

</details>

<details>
<summary><b>Delivery events — nine more</b> (optional; read before granting)</summary>

Everything above is scoped to SES and mostly read-only. This adds permission to
**create and delete SNS topics and SQS queues** in your account — a different
kind of trust, and the reason it is a separate policy and a separate button
rather than something the connect flow quietly needs.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sqs:CreateQueue",
        "sqs:GetQueueAttributes",
        "sqs:SetQueueAttributes",
        "sqs:DeleteQueue",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage"
      ],
      "Resource": "arn:aws:sqs:*:*:seskit-events"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sns:CreateTopic",
        "sns:Subscribe",
        "sns:Unsubscribe",
        "sns:DeleteTopic"
      ],
      "Resource": "arn:aws:sns:*:*:seskit-events"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ses:CreateConfigurationSet",
        "ses:DeleteConfigurationSet",
        "ses:CreateConfigurationSetEventDestination",
        "ses:UpdateConfigurationSetEventDestination",
        "ses:DeleteConfigurationSetEventDestination"
      ],
      "Resource": "*"
    }
  ]
}
```

The SQS and SNS statements are scoped by resource to the queue and topic SESKit
creates, so this policy cannot touch anything else you own even by mistake. If
you change `EVENT_RESOURCE_PREFIX`, change those ARNs to match.

The delete permissions are there so that removing event reporting, or
disconnecting the account, actually cleans up. Granting create without delete
would leave SESKit able to make resources in your account and unable to tidy
them away.

`sqs:SendMessage` is deliberately absent. SESKit never writes to the queue —
SNS does, under a queue policy SESKit sets during setup that admits that one
topic and nothing else. Adding it here would grant a permission nothing uses.

</details>

### Verify a sender

Amazon SES will not send from an address it has not verified, so before your
first real send you need at least one identity on the **Domains** page.

| | Setup | Time | Sends as |
|---|---|---|---|
| **Email address** | Click a link SES mails you | Minutes | That one address |
| **Domain** | Three CNAME records at your DNS host | Up to 72h | Anything on the domain |

The address form matters out of proportion to its size: it needs no DNS and no
registrar access, so it is the fastest way to reach a real send — worth doing
first even if you intend to use a domain.

SESKit re-checks unverified identities every few hours and verified ones monthly.
That last one is deliberate: it notices if a DKIM record is removed long after
setup, which would otherwise look fine right up until a send failed.

### The SES sandbox

Every new AWS account is in the SES sandbox: 200 messages per 24 hours, one per
second, and **only to verified recipients**. SESKit detects this on connect and
says so on the AWS page until the account graduates. If your first send fails
with a rejected recipient, this is almost always why —
[request production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html)
to lift the limits.

Sandbox status and quota are read from AWS when you connect and when you press
Refresh, not on every page load. The page says when it last checked.

---

## Delivery events

`sent` means Amazon SES accepted the message. That is the last thing SESKit can
observe on its own — whether it *arrived*, bounced, or was reported as spam is
knowledge that lives at AWS, and SES does not report it anywhere unless asked.

Press **Set up event reporting** on the AWS page. SESKit creates, in the region
the project is connected to:

| Resource | Purpose |
|---|---|
| SQS queue `seskit-events` | the worker polls this |
| SNS topic `seskit-events` | SES publishes here; it fans out to the queue |
| SES configuration set `seskit` | sends name it, or SES publishes nothing |

Messages sent from that point on show a delivery history: delivered, bounced
with the reason SES gave, marked as spam.

> [!WARNING]
> **Messages sent before setup gain nothing.** SES only reports on mail sent
> through a configuration set, so existing messages have no history and never
> will. The message page distinguishes that from "no events yet" rather than
> showing an ambiguous blank.

Removing event reporting, or disconnecting the account, deletes what was
created. Nothing else in your account is touched, and if a second project on the
same instance shares the region, the infrastructure stays until the last one
stops using it.

### Two ways events get back

Set by `EVENT_INGESTION`:

| Mode | How | When to use |
|---|---|---|
| **`sqs`** *(default)* | The worker polls the queue | Anywhere. No inbound port, no public hostname, no certificate — AWS cannot POST to a laptop |
| **`https`** | SNS posts to `POST /v1/events/ses` | A public address with TLS. Lower latency, no polling. Needs `PUBLIC_BASE_URL` |
| **`both`** | Both at once | Migrating between the two |

The HTTPS endpoint is unauthenticated by necessity: SNS has no credential to
present. Every request is verified against the RSA signature SNS signed it with,
using a certificate fetched only from `sns.<region>.amazonaws.com` and only
after that host is validated. An unsigned or altered notification is refused
with 403 and nothing is recorded. The endpoint is not mounted at all unless
`EVENT_INGESTION` asks for it.

### Open and click tracking

Off unless you turn it on, per project, and worth understanding before you do.

> [!CAUTION]
> Enabling it asks Amazon SES to **rewrite every link in the mail this project
> sends** so clicks route through an Amazon-operated domain, and to add an
> invisible tracking pixel to HTML messages. Your recipients see the rewritten
> links. That is a visible change to your own product with privacy consequences,
> which is why it is a deliberate choice rather than a default.

### Duplicate events

SNS and SQS are both explicitly at-least-once, so the same notification will
arrive twice sooner or later. SESKit deduplicates on the SNS message id with a
unique constraint, so a redelivered bounce is recorded once. This matters more
than it sounds: a double-counted bounce inflates the rate AWS judges your
account by.

---

## Webhooks

Delivery events tell SESKit what happened to a message. Webhooks tell *your
application*, so it can suppress a bounced address or flag a complaint without
polling `GET /v1/emails/{id}` for everything it has ever sent.

Add an endpoint on the **Webhooks** page. SESKit then POSTs a signed JSON body
for each of §16's six event types:

```text
email.sent   email.delivered   email.bounced
email.opened email.clicked     email.complained
```

The body is the same normalised event the dashboard stores:

```json
{
  "id": "evt_01J8XQ...",
  "type": "email.bounced",
  "email_id": "email_01J8XQ...",
  "created_at": "2026-09-02T09:00:05+00:00",
  "data": {
    "to": ["user@example.com"],
    "bounce_type": "Permanent",
    "diagnostic": "smtp; 550 5.1.1 user unknown"
  }
}
```

> [!IMPORTANT]
> Webhooks need [delivery events](#delivery-events) set up first. Without a
> configuration set, Amazon SES publishes nothing, so there is nothing to
> forward.

### Verifying the signature

**Verify before you act on a webhook.** The URL is the only thing an attacker
needs to guess, and acting on a forged `email.bounced` means suppressing an
address they chose.

Each request carries two headers:

```text
X-SESKit-Signature: v1=3f7a...
X-SESKit-Timestamp: 1756800000
```

Recompute the HMAC-SHA256 over `"{timestamp}.{body}"` using your endpoint's
signing secret, shown on the Webhooks page:

```python
import hashlib, hmac, time


def verify(secret: str, body: bytes, signature: str, timestamp: str) -> bool:
    # Reject anything stale, or a captured request replays forever.
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"v1={expected}", signature)
```

Three things matter and are easy to get wrong:

- **Use the raw request body**, not a re-serialised copy. Parsing the JSON and
  dumping it again reorders keys or changes separators, and the signature will
  never match.
- **The timestamp is inside the signed string**, which is what makes a replay
  detectable. Check it, or a request captured once is valid forever.
- **Compare in constant time.** A byte-at-a-time comparison leaks the correct
  signature to anyone willing to make enough attempts.

The `v1=` prefix exists so the scheme can change later without a flag day.

### Retries and failure

| | |
|---|---|
| **Retried** | 5xx, 429, timeouts, connection failures |
| **Not retried** | 4xx — the endpoint understood and refused |
| **Backoff** | `5s × 2ⁿ` with 30% jitter, six attempts, ≈5 minutes total |
| **Auto-disable** | 10 consecutive failures, reset by any success |

An endpoint SESKit switches off gets a status of its own — the page says it
stopped and why, rather than showing a switch that appears to have moved by
itself. Re-enabling clears the failure count.

Deliveries are **at-least-once**: the same event can arrive twice if your
endpoint is slow to answer. Deduplicate on the event `id`.

> [!CAUTION]
> Endpoint URLs are validated against SSRF, at registration **and again at
> every delivery** against the resolved address. Loopback, private and
> link-local addresses are refused outside local development, and redirects are
> never followed — a redirect would forward your signed payload to a host you
> never registered. If you genuinely need an internal destination in
> production, list the range in `WEBHOOK_ALLOWED_CIDRS`.

---

## API

Authenticate with `Authorization: Bearer sk_...`. Interactive docs at
`/docs`; the OpenAPI schema at `/openapi.json`.

| Method | Path | |
|---|---|---|
| `POST` | `/v1/emails` | Send an email. Accepts `Idempotency-Key` |
| `GET` | `/v1/emails/{email_id}` | Retrieve one email and its status |
| `GET` | `/v1/domains` | List sending identities and their verification state |
| `GET` | `/v1/api-keys` | List this project's keys (never the secrets) |
| `GET` | `/v1/webhooks` | List webhook endpoints (never the signing secrets) |
| `GET` | `/v1/webhooks/{endpoint_id}/deliveries` | Recent delivery attempts and their status |
| `POST` | `/v1/events/ses` | SNS notification receiver. Not for callers — see [delivery events](#delivery-events) |

Errors use a consistent envelope:

```json
{ "error": { "type": "domain_not_verified", "message": "..." } }
```

Rate limits are per project and reported on every response via
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.

---

## Configuration

Everything comes from the environment, or a local `.env`. See
[`.env.example`](.env.example) for the annotated full list.

| Variable | Default | |
|---|---|---|
| `SECRET_KEY` | *required* | Signs sessions. Refuses to boot on the placeholder value |
| `DATABASE_URL` | *required* | PostgreSQL, async driver |
| `REDIS_URL` | *required* | Caching, rate limits, the job queue |
| `ALLOW_SIGNUP` | `false` | Registration always opens while no account exists, then closes |
| `AWS_DEFAULT_REGION` | `us-east-1` | Pre-selects the region in the connect form |
| `EVENT_INGESTION` | `sqs` | `sqs` · `https` · `both` |
| `EVENT_RESOURCE_PREFIX` | `seskit` | Names the SQS queue and SNS topic |
| `EVENT_CONFIGURATION_SET` | `seskit` | The SES configuration set sends go through |
| `PUBLIC_BASE_URL` | — | Where SNS can reach this instance, for `https` ingestion |
| `WEBHOOK_ALLOWED_CIDRS` | — | Internal ranges webhooks may reach in production |
| `WEBHOOK_MAX_ATTEMPTS` | `6` | Delivery attempts before a webhook is abandoned |
| `SMTP_HOST` | — | Local delivery target. Points at Mailpit in development |
| `API_RATE_LIMIT_PER_MINUTE` | `100` | Per project, not per key |

> [!NOTE]
> There is deliberately no `AWS_ACCESS_KEY_ID` setting. Credentials are resolved
> by boto3 from the environment SESKit runs in and are never handled as data —
> naming them here would invite them into logs and into config dumps.

---

## Architecture

```text
apps/
  api/                  FastAPI app, Jinja2 templates, static assets
  worker/               ARQ background worker
packages/
  core/                 Config, logging, persistence, shared domain logic
  provider-aws-ses/     Amazon SES provider
  provider-smtp/        SMTP provider, for local delivery
  sdk-python/           Python SDK                            (planned)
migrations/             Alembic
scripts/                Repository tooling
.githooks/              Shared git hooks
docs/                   Design system, prior art, conventions
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends on
the other. Provider-specific code stays inside its own package and never leaks
into the API or core — `core` defines the provider interface and chooses which
one to use, but imports neither, so the dependency only ever points one way.

**Stack** — Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) ·
Alembic · PostgreSQL · Redis · ARQ · structlog · Jinja2 · HTMX · hand-written
CSS · uv workspaces · Docker Compose

---

## Contributing

Contributions are welcome. Start with
[**CONTRIBUTING.md**](CONTRIBUTING.md) — it covers the development setup, the
git hooks, the commit convention, and the two Docker traps that will otherwise
cost you an afternoon.

```bash
uv sync                        # install
uv run pytest                  # tests
uv run ruff check .            # lint
uv run ruff format .           # format
uv run mypy .                  # type-check
```

Before changing the dashboard, read
[`docs/design-system.md`](docs/design-system.md).

Security issues: please see [SECURITY.md](SECURITY.md) rather than opening a
public issue.

---

## License

[MIT](LICENSE) © SESKit contributors
