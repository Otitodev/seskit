<div align="center">

<img src="docs/assets/seskit-icon.png" alt="" width="88" height="88">

# SESKit

**A self-hosted developer email platform built on Amazon SES.**

The API, dashboard, delivery events, webhooks and suppression a hosted email
service sells you — running in your own AWS account, on your own server.

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

## Features

- **Send API** — `POST /v1/emails` with attachments, custom headers,
  idempotency keys. Answers in milliseconds; a worker talks to SES.
- **Python SDK** — `pip install seskit`. Typed, sync and async, optional.
- **Bring your own AWS** — paste an access key per project; it is verified
  against AWS and stored encrypted. Different projects, different accounts.
- **Sender verification** — addresses and domains, with the DKIM records to
  add and automatic re-checking.
- **Delivery events** — delivered, bounced, complained, opened, clicked.
  Polled over SQS by default, so it works behind NAT with no inbound port.
- **Webhooks** — signed, retried with backoff, delivery log per endpoint.
- **Suppression** — hard bounces and complaints stop future sends; RFC 8058
  one-click unsubscribe.
- **Dashboard** — delivery metrics, message timelines, a test-send form.
  Server-rendered, no JavaScript build.
- **One image** — Python + PostgreSQL + Redis. Runs anywhere Docker does.

## Get started

Requires [Docker](https://docs.docker.com/get-docker/). No AWS account
needed to try it — mail is captured by
[Mailpit](https://mailpit.axllent.org/) instead of sent.

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
docker compose up
```

Dashboard at <http://localhost:8000>, inbox at <http://localhost:8025>. The
first registration claims the instance. Create an API key, then send:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"from":"hello@example.com","to":["you@example.com"],
       "subject":"Hello","html":"<h1>It works</h1>"}'
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

**On a server**, one line installs it and generates a real `SECRET_KEY`:

```bash
curl -fsSL https://otitodev.github.io/seskit/install.sh | sh
```

Behind a TLS proxy or on a managed platform? See
[deploying](https://otitodev.github.io/seskit/operating/deploying/).

## How it works

```text
your application                    your server
┌──────────────────────┐           ┌──────────────────────────┐
│ client.emails.send() │  ─HTTP→   │ API + dashboard          │
│                      │           │ worker  ──────────────── │ ──→ Amazon SES
└──────────────────────┘           │ PostgreSQL · Redis       │
                                   └──────────────────────────┘
```

Two processes from one image. The API records a message and queues a job; the
worker sends it, delivers webhooks, and polls for delivery events.

Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL · Redis · ARQ · HTMX.
No Node.js, anywhere.

## Status

Pre-release. Every candidate is tagged and
[documented](https://github.com/Otitodev/seskit/releases). What remains before
`v0.1.0` is stated in each release's notes.

## Contributing

[**CONTRIBUTING.md**](CONTRIBUTING.md) has the setup, checks and commit
convention. [`AGENTS.md`](AGENTS.md) orients a coding agent. Security issues
go to [SECURITY.md](SECURITY.md), not a public issue.

## License

[MIT](LICENSE) © SESKit contributors
