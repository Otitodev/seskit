# SESKit

**A Python-native developer email platform built on Amazon SES.**

SESKit makes Amazon SES feel as simple to use as Resend. Connect an AWS
account, verify a domain, create an API key, send email — and see what
happened.

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_xxx")

client.emails.send(
    from_="hello@example.com",
    to=["user@example.com"],
    subject="Welcome",
    html="<h1>Hello!</h1>",
)
```

> **AWS should handle email infrastructure. SESKit should handle developer
> experience.**

---

## Why SESKit

Open-source Resend-on-SES platforms already exist, and they are good — but they
are JavaScript end to end. SESKit is for teams who would rather not add a Node
toolchain to their stack to self-host their email infrastructure:

- **A FastAPI backend and a first-class Python SDK**, not a TypeScript-only client.
- **One service to run.** The dashboard is server-rendered by the same Python
  process that serves the API — no separate frontend service, no `node_modules`,
  no JavaScript build step.
- **Your AWS account, your costs.** SESKit is the control plane; Amazon SES does
  the delivery.

---

## Status

**Phase 7 of 11 — delivery events.** SESKit sends email and reports what
happened to it. Accounts, API keys, AWS connection, sender verification, the
send pipeline and delivery events all work; customer webhooks are next. See
[`SESKit_MVP.md`](SESKit_MVP.md) §31 for the full build order.

| Phase | | |
|---|---|---|
| 1 | Project foundation | ✅ Done |
| 2 | Authentication and projects | ✅ Done |
| 3 | API keys | ✅ Done |
| 4 | AWS SES provider | ✅ Done |
| 5 | Domain management | ✅ Done |
| 6 | Email API | ✅ Done |
| 7 | Event processing | ✅ Done |
| 8 | Webhooks | Not started |
| 9 | Dashboard | Not started |
| 10 | Python SDK | Not started |
| 11 | Hardening | Not started |

---

## Connecting AWS

SESKit never stores AWS credentials. It resolves them the standard boto3 way, in
boto3's own order of precedence:

1. an IAM role attached to the EC2 instance, ECS task, or EKS pod
2. the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
3. a shared credentials file (`~/.aws/credentials`)
4. SSO or workload identity where configured

Give the process credentials by whichever of those suits your deployment, then
open **AWS** in the dashboard, choose your SES region, and connect. SESKit asks
AWS who the identity is and what it may do, and records the answer. Connecting
creates nothing in your AWS account.

Setting up delivery events does — a queue, a topic and a configuration set — but
that is a separate button, it tells you what it will create first, and
disconnecting removes them again. See
[Delivery events](#delivery-events) below.

### Minimum IAM policy

§9 of the spec is explicit that SESKit must never ask for `AdministratorAccess`.
There are two policies here because they grant genuinely different things, and
you should be able to run SESKit without the second.

#### Sending (six actions)

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

#### Delivery events (nine more)

**Read this before granting it.** Everything above is scoped to SES and mostly
read-only. This adds permission to create and delete SNS topics and SQS queues
in your account — a different kind of trust, and the reason it is a separate
policy and a separate button rather than something the connect flow quietly
needs.

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

### Verifying a sender

Amazon SES will not send from an address it has not verified, so before your
first send you need at least one identity on the **Domains** page.

There are two kinds, and the difference matters if you are in a hurry:

- **An email address** verifies in minutes and needs no DNS at all. SES mails
  the address a link; click it and you can send. This is the fastest way to get
  something real working, and it is worth doing first even if you intend to use
  a domain.
- **A domain** lets you send from any address on it, and needs three CNAME
  records at your DNS provider. SESKit shows them, and re-checks on its own —
  there is nothing to press once they are live. DNS can take up to 72 hours to
  propagate, though most providers are much quicker.

SESKit re-checks unverified identities every few hours and verified ones monthly.
That last one is deliberate: it notices if a DKIM record is removed long after
setup, which would otherwise look fine until a send failed.

### Delivery events

`sent` means Amazon SES accepted the message. That is the last thing SESKit can
observe on its own — whether it *arrived*, bounced, or was reported as spam is
knowledge that lives at AWS, and SES does not report it anywhere unless you ask.

Press **Set up event reporting** on the AWS page. SESKit creates, in the region
the project is connected to:

| | |
|---|---|
| SQS queue `seskit-events` | the worker polls this |
| SNS topic `seskit-events` | SES publishes here, and it fans out to the queue |
| SES configuration set `seskit` | sends name it, or SES publishes nothing |

Messages sent from that point on show a delivery history: delivered, bounced
with the reason SES gave, marked as spam. **Messages sent before setup gain
nothing** — SES only reports on mail sent through a configuration set, so
existing messages have no history and never will. The message page says which of
those two situations it is in rather than showing an ambiguous blank.

Removing event reporting, or disconnecting the account, deletes what was
created. Nothing else in your account is touched, and if a second project on the
same instance shares the region, the infrastructure stays until the last one
stops using it.

#### Two ways events get back

Set by `EVENT_INGESTION`:

- **`sqs`** (the default) — the worker polls the queue. Works anywhere: no
  inbound port, no public hostname, no certificate. AWS cannot POST to a laptop,
  so this is what makes a self-hosted install work at all.
- **`https`** — SNS posts to `POST /v1/events/ses`. Needs a public address, set
  in `PUBLIC_BASE_URL`, and a certificate. Lower latency and no polling.
- **`both`** — for a migration between the two.

The HTTPS endpoint is unauthenticated by necessity: SNS has no credential to
present. Every request is checked against the RSA signature SNS signed it with,
using a certificate fetched only from `sns.<region>.amazonaws.com` and only
after that host is validated. An unsigned or altered notification is refused
with 403 and nothing is recorded. The endpoint is not mounted at all unless
`EVENT_INGESTION` asks for it.

#### Open and click tracking

Off unless you turn it on, per project, and worth understanding before you do.
Enabling it asks Amazon SES to **rewrite every link in the mail this project
sends** so that clicks route through an Amazon-operated domain, and to add an
invisible tracking pixel to HTML messages. Your recipients see the rewritten
links. That is a visible change to your own product with privacy consequences,
which is why it is a deliberate choice rather than a default.

#### Duplicate events

SNS and SQS are both explicitly at-least-once, so the same notification will
arrive twice sooner or later. SESKit deduplicates on the SNS message id with a
unique constraint, so a redelivered bounce is recorded once. This matters more
than it sounds: a double-counted bounce inflates the rate AWS judges your
account by.

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

## Quickstart

Requires [Docker](https://docs.docker.com/get-docker/). **No AWS account
needed** — local email is captured by [Mailpit](https://mailpit.axllent.org/)
instead of being sent through SES.

```bash
git clone <repo> && cd seskit
cp .env.example .env
docker compose up
```

| | |
|---|---|
| Dashboard | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Mailpit inbox | http://localhost:8025 |

Postgres and Redis are published on **55432** and **56379** rather than their
standard ports. Machines with PostgreSQL installed frequently already have
clusters on 5432 *and* 5433; those bind before Docker does, and the container
then looks healthy while every connection quietly reaches the wrong database.
Override with `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` if you prefer.

### Send your first email

Nothing above needed an AWS account, and neither does this. A project with no
AWS connection sends through Mailpit, so you can watch a real message go through
the whole pipeline before deciding whether SESKit is worth configuring.

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

### Going to production

Sending switches from Mailpit to Amazon SES for a project once two things are
true: an AWS account is connected, and the `from` address is covered by a
verified identity. Nothing changes in your code — same endpoint, same request.

If a project has connected AWS but the sender is not verified, the send is
**refused** rather than quietly delivered locally. That would report success
while the message reached nobody.

Bear in mind a new AWS account starts in the SES sandbox and can only mail
verified addresses until AWS grants production access, which takes about a day.
Verifying your own email address takes minutes and needs no DNS, so it is the
fastest way to get a real send working — see
[Verifying a sender](#verifying-a-sender) above.

Set up [delivery events](#delivery-events) at the same time. Without them a
message reads as `sent` and stops there, so a bounced address or a spam
complaint is invisible — and those are the two things that decide whether AWS
keeps letting you send.

### Working without Docker

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

## Development

```bash
uv sync                        # install
uv run pytest                  # tests
uv run ruff check .            # lint
uv run ruff format .           # format
uv run mypy .                  # type-check
```

### Git hooks

One command, once per clone:

```bash
git config core.hooksPath .githooks
git config commit.template .gitmessage    # optional, but recommended
```

That installs two checks:

- **`pre-commit`** — runs ruff, mypy, and the hygiene checks from
  `.pre-commit-config.yaml`.
- **`commit-msg`** — enforces the commit convention below.

Use `core.hooksPath`, not `pre-commit install`: pre-commit refuses to install
while `core.hooksPath` is set, and going around it would bypass the chaining
described next.

> **Your global hooks keep working.** Git runs exactly one hook per event, so
> pointing `core.hooksPath` at this repo would normally disable anything you
> have configured globally — silently. Both hooks in `.githooks/` therefore run
> your global hook of the same name first, and only then the SESKit check.

Skipping a hook in an emergency: `git commit --no-verify`. CI runs the same
checks, so it will still be caught.

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>

Why:  ...
What: ...

Refs: SESKit_MVP.md §12
```

```text
feat(api): add idempotency key support
fix(worker): retry webhook delivery on connection reset
docs: explain the SES sandbox in the quickstart
feat(api)!: drop the v0 send endpoint          # ! marks a breaking change
```

Subject lines are imperative ("add", not "added"), lower case (acronyms like
SES and DKIM are fine), no trailing period, 72 characters max. The body should
explain **why** — the diff already shows what.

Scopes match the repository layout: `api`, `ui`, `worker`, `core`,
`provider-ses`, `sdk`, `migrations`, `docker`, `ci`, `deps`, `docs`, `release`.
Omit the scope for repo-wide changes.

Blocked by the hook? Your message is kept in `.git/COMMIT_EDITMSG` — reopen it
with `git commit -eF .git/COMMIT_EDITMSG`.

Full reference, including troubleshooting and how the hook chaining works:
[`docs/commit-conventions.md`](docs/commit-conventions.md).

### Repository layout

```text
apps/
  api/        FastAPI app, Jinja2 templates, static assets
  worker/     ARQ background worker
packages/
  core/       Config, logging, persistence, shared domain logic
  provider-aws-ses/   Amazon SES provider
  provider-smtp/      SMTP provider, for local delivery
  sdk-python/         Python SDK                 (Phase 10)
migrations/   Alembic
scripts/      Repository tooling (commit message check)
.githooks/    Shared git hooks
docs/         design-system.md and friends
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends on
the other. Provider-specific code stays inside its own package and never leaks
into the API or core — `core` defines the provider interface and chooses which
one to use, but imports neither, so the dependency only ever points one way.

Adding a workspace package means registering it in `docker/Dockerfile` too: the
image copies each member's manifest by hand to keep the dependency layer cached,
and a package missing from that list fails at container start rather than at
build time.

Adding a *dependency* to an existing package has a second trap. Compose mounts
an anonymous volume at `/app/.venv` so the bind-mounted sources do not shadow
the installed environment — and that volume survives `docker compose build`, so
a rebuilt image still starts with the old `.venv` and the new dependency is
missing. The symptom is a `ModuleNotFoundError` for a package that is provably
present in the image. Renew the volume:

```bash
docker compose up -d --renew-anon-volumes
```

### Stack

Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic ·
PostgreSQL · Redis · ARQ · structlog · Jinja2 · HTMX · hand-written CSS ·
uv workspaces · Docker Compose

Before changing the dashboard, read [`docs/design-system.md`](docs/design-system.md).

---

## License

MIT — see [LICENSE](LICENSE).
