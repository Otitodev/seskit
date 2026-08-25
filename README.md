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

**Phase 1 of 11 — project foundation.** The stack runs, but there is no email
sending yet. See [`SESKit_MVP.md`](SESKit_MVP.md) §31 for the full build order.

| Phase | | |
|---|---|---|
| 1 | Project foundation | ✅ Done |
| 2 | Authentication and projects | Not started |
| 3 | API keys | Not started |
| 4 | AWS SES provider | Not started |
| 5 | Domain management | Not started |
| 6 | Email API | Not started |
| 7 | Event processing | Not started |
| 8 | Webhooks | Not started |
| 9 | Dashboard | Not started |
| 10 | Python SDK | Not started |
| 11 | Hardening | Not started |

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
