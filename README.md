<div align="center">

<img src="docs/assets/seskit-icon.png" alt="" width="88" height="88">

# SESKit

**A self-hosted developer email platform built on Amazon SES.**

Connect an AWS account, verify a sender, create an API key, send email —
and see what happened to it.

[![CI](https://github.com/Otitodev/seskit/actions/workflows/ci.yml/badge.svg)](https://github.com/Otitodev/seskit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/seskit.svg)](https://pypi.org/project/seskit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

[Documentation](https://otitodev.github.io/seskit/) ·
[Install](https://otitodev.github.io/seskit/getting-started/installation/) ·
[Your first email](https://otitodev.github.io/seskit/getting-started/first-email/) ·
[HTTP API](https://otitodev.github.io/seskit/reference/api/)

</div>

<!-- screenshot: docs/assets/dashboard.png — the Overview page, light theme -->

## Why

Amazon SES is the cheapest reliable way to send email, and the least pleasant
to use directly. Everything around it — verifying senders, getting delivery
events back, webhooks, suppression, a dashboard — is what a hosted email API
sells you. SESKit is that layer, running in your own AWS account, on your own
server.

**AWS handles email infrastructure. SESKit handles developer experience.**

## Install

Requires [Docker](https://docs.docker.com/get-docker/). No AWS account is
needed to try it — local mail is captured by
[Mailpit](https://mailpit.axllent.org/) instead of being sent.

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
docker compose up
```

Dashboard at <http://localhost:8000>, Mailpit inbox at <http://localhost:8025>.
The first registration claims the instance.

On a server, one line does the same and generates a real `SECRET_KEY`:

```bash
curl -fsSL https://otitodev.github.io/seskit/install.sh | sh
```

Deploying somewhere managed, or behind a TLS proxy? The
[deploying guide](https://otitodev.github.io/seskit/operating/deploying/)
covers the two settings that matter.

## Send

Create an API key on the dashboard, then:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"from":"hello@example.com","to":["you@example.com"],
       "subject":"Hello","html":"<h1>It works</h1>"}'
```

Or from Python:

```bash
pip install seskit
```

```python
from seskit import SesKit

client = SesKit(api_key="sk_...", base_url="http://localhost:8000")
client.emails.send(
    from_="hello@example.com",
    to="you@example.com",
    subject="Hello",
    html="<h1>It works</h1>",
)
```

The SDK is a thin, typed wrapper over the same HTTP API — sync and async, with
every refusal as a class you can catch. It is optional.

## Features

- **Sending** — `POST /v1/emails` with attachments, custom headers,
  idempotency keys, and queued delivery. A send answers in milliseconds; the
  worker talks to SES.
- **Per-project AWS accounts** — paste an access key into the dashboard. It is
  checked against AWS before it is stored, and kept encrypted. Two projects on
  one instance can send through two AWS accounts.
- **Sender verification** — email addresses and domains, with the DKIM
  records to add and automatic re-checking.
- **Delivery events** — delivered, bounced, complained, opened, clicked; via
  SQS by default, so it works behind NAT with no inbound port.
- **Webhooks** — signed, retried with backoff, with a delivery log per
  endpoint.
- **Suppression** — hard bounces and complaints stop future sends
  automatically; RFC 8058 one-click unsubscribe.
- **Dashboard** — server-rendered, no JavaScript build step: delivery metrics,
  message history with timelines, a test-send form.
- **Runs anywhere Docker does** — one Python service plus PostgreSQL and
  Redis. Strict CSP, request size caps, and a `doctor` that names the one
  thing to fix.

## How it fits together

```text
your application                    your server
┌──────────────────────┐           ┌──────────────────────────┐
│ pip install seskit   │  ─HTTP→   │ API + dashboard          │
│ client.emails.send() │           │ worker  ──────────────── │ ──→ Amazon SES
└──────────────────────┘           │ PostgreSQL · Redis       │
                                   └──────────────────────────┘
```

Two processes from one image. The API records a message and queues a job; the
worker sends it, delivers webhooks, and polls for delivery events. They never
talk to each other directly — which is why a send answers `queued`, and why
the worker needs no inbound port.

**Stack** — Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL · Redis · ARQ ·
HTMX · hand-written CSS. No Node.js, anywhere.

## Status

Pre-release. Every candidate is tagged and
[documented](https://github.com/Otitodev/seskit/releases); a real message has
gone through SES from a real deployment. What remains before `v0.1.0` is
stated in each release's notes.

## Contributing

[**CONTRIBUTING.md**](CONTRIBUTING.md) has the setup, the checks, and the
commit convention. Working with a coding agent? [`AGENTS.md`](AGENTS.md) is
the orientation. Security issues: [SECURITY.md](SECURITY.md), not a public
issue.

## License

[MIT](LICENSE) © SESKit contributors
