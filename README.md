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

## Two halves, and only one of them is the product

```text
your application                   your server
┌─────────────────────┐           ┌────────────────────────┐
│ pip install seskit  │  ─HTTP→   │ git clone + compose up │ ──→ Amazon SES
│ client.emails.send()│           │ API, dashboard, worker │
└─────────────────────┘           └────────────────────────┘
        the client                        the server
```

**This repository is the server.** You clone it and run it once, on your own
machine or your own infrastructure. That is SESKit.

**`pip install seskit` is a client** that goes inside your application and
makes HTTP calls to a running server — and it is optional, because it is a thin
wrapper over the same HTTP API `curl` reaches. The package currently reserves
the name and contains no working client; **use the HTTP API**.

## Quickstart

Requires [Docker](https://docs.docker.com/get-docker/). **No AWS account
needed** — local mail is captured by [Mailpit](https://mailpit.axllent.org/)
instead of being sent through SES.

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
docker compose up
```

Dashboard at <http://localhost:8000>, Mailpit inbox at <http://localhost:8025>.
Create the owner account, make an API key, and send:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"from":"hello@example.com","to":["you@example.com"],
       "subject":"Hello from SESKit","html":"<h1>It works</h1>"}'
```

Full walkthrough: [Your first email](docs/getting-started/first-email.md).

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
| ✅ | Analytics dashboard | Six counts, five rates, activity chart |
| ⬜ | Suppression list | Next — bounce rates are shown, not yet acted on |
| ⬜ | Python SDK | Currently a stub — use the HTTP API |
| ⬜ | Production hardening | |

Build order: [`SESKit_MVP.md`](SESKit_MVP.md) §31.

## Documentation

The full documentation lives in [`docs/`](docs/index.md).

**Getting started** — [install and run](docs/getting-started/installation.md) ·
[your first email](docs/getting-started/first-email.md) ·
[sending from your app](docs/getting-started/sending-from-your-app.md) ·
[the dashboard](docs/getting-started/dashboard-tour.md)

**Guides** — [no AWS account yet?](docs/guides/no-aws-account-yet.md) ·
[connect AWS](docs/guides/connect-aws.md) ·
[IAM policies](docs/guides/iam-policies.md) ·
[verify a sender](docs/guides/verify-a-sender.md) ·
[the SES sandbox](docs/guides/ses-sandbox.md) ·
[delivery events](docs/guides/delivery-events.md) ·
[webhooks](docs/guides/webhooks.md) ·
[reading your metrics](docs/guides/metrics.md)

**Reference** — [HTTP API](docs/reference/api.md) ·
[configuration](docs/reference/configuration.md) ·
[events](docs/reference/events.md) · [errors](docs/reference/errors.md)

**Operating** — [deploying](docs/operating/deploying.md) ·
[upgrading](docs/operating/upgrading.md) ·
[backup](docs/operating/backup.md) ·
[troubleshooting](docs/operating/troubleshooting.md)

**Design notes** — [why SES-native](docs/design/why-ses.md) ·
[security model](docs/design/security-model.md) ·
[prior art](docs/prior-art.md)

### The two questions asked before anyone clones

- **What permissions does it need?** Six SES actions to send, nine more for
  delivery events, and never `AdministratorAccess` —
  [IAM policies](docs/guides/iam-policies.md).
- **Can I send straight away?** Not to arbitrary recipients: every new AWS
  account is in the SES sandbox for roughly 24 hours. Which is why the
  quickstart above needs no AWS account at all —
  [the SES sandbox](docs/guides/ses-sandbox.md).

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
docs/                   Documentation site
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends on
the other. Provider-specific code stays inside its own package and never leaks
into the API or core.

**Stack** — Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) ·
Alembic · PostgreSQL · Redis · ARQ · structlog · Jinja2 · HTMX · hand-written
CSS · uv workspaces · Docker Compose

## Contributing

Start with [**CONTRIBUTING.md**](CONTRIBUTING.md) — development setup, the git
hooks, the commit convention, and the two Docker traps that will otherwise cost
you an afternoon.

```bash
uv sync                        # install
uv run pytest                  # tests
uv run ruff check .            # lint
uv run mypy .                  # type-check
```

Before changing the dashboard, read
[`docs/design-system.md`](docs/design-system.md).

Security issues: see [SECURITY.md](SECURITY.md) rather than opening a public
issue.

## License

[MIT](LICENSE) © SESKit contributors
