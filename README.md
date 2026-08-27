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

**Phase 4 of 11 — AWS SES provider.** The stack runs, accounts and API keys
work, and an AWS account can be connected and inspected — but there is still no
email sending. That is Phase 6. See [`SESKit_MVP.md`](SESKit_MVP.md) §31 for the
full build order.

| Phase | | |
|---|---|---|
| 1 | Project foundation | ✅ Done |
| 2 | Authentication and projects | ✅ Done |
| 3 | API keys | ✅ Done |
| 4 | AWS SES provider | ✅ Done |
| 5 | Domain management | Not started |
| 6 | Email API | Not started |
| 7 | Event processing | Not started |
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
AWS who the identity is and what it may do, and records the answer. It creates
nothing in your AWS account.

### Minimum IAM policy

§9 of the spec is explicit that SESKit must never ask for `AdministratorAccess`.
For Phase 4 — connecting and inspecting an account — these two actions are
enough:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity", "ses:GetAccount"],
      "Resource": "*"
    }
  ]
}
```

Neither action can send mail or change anything, so this policy is safe to grant
before you have decided to send through SESKit at all. Later phases need more —
domain identities in Phase 5, sending in Phase 6 — and this section grows with
them rather than asking for those permissions up front.

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
  provider-aws-ses/   Amazon SES provider        (Phase 4)
  sdk-python/         Python SDK                 (Phase 10)
migrations/   Alembic
scripts/      Repository tooling (commit message check)
.githooks/    Shared git hooks
docs/         design-system.md and friends
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends on
the other. Provider-specific code stays inside its own package and never leaks
into the API or core.

### Stack

Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic ·
PostgreSQL · Redis · ARQ · structlog · Jinja2 · HTMX · hand-written CSS ·
uv workspaces · Docker Compose

Before changing the dashboard, read [`docs/design-system.md`](docs/design-system.md).

---

## License

MIT — see [LICENSE](LICENSE).
