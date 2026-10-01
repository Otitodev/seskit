<div align="center">

<img src="docs/assets/seskit-icon.png" alt="" width="88" height="88">

# SESKit

**Self-hosted email platform built on Amazon SES.**

Run it on your own server, connect your AWS account, and get an email API
and dashboard.

[![CI](https://github.com/Otitodev/seskit/actions/workflows/ci.yml/badge.svg)](https://github.com/Otitodev/seskit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/seskit.svg)](https://pypi.org/project/seskit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Docs](https://otitodev.github.io/seskit/) ·
[Install](https://otitodev.github.io/seskit/getting-started/installation/) ·
[First email](https://otitodev.github.io/seskit/getting-started/first-email/) ·
[API](https://otitodev.github.io/seskit/reference/api/)

</div>

## Features

- **Send email** through a REST API or the Python SDK
- **Verify senders** — email addresses and domains, with the DNS records to add
- **Delivery events** — see whether each message was delivered, bounced, opened or clicked
- **Webhooks** — get notified when something happens to a message
- **Suppression list** — bounced and unsubscribed addresses are never sent to again
- **Dashboard** — send a test message, watch deliveries, manage everything
- **Request SES production access** from the dashboard

## Run it

You need Docker.

```bash
curl -fsSL https://otitodev.github.io/seskit/install.sh | sh
```

Open `http://<your-server>:8000`, create your account, and paste in an AWS
access key. Put a TLS proxy in front before sending real mail.

To try it locally without an AWS account, mail is captured by Mailpit:

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
docker compose up
```

## Send email

Create an API key in the dashboard, then:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"from":"hello@example.com","to":["you@example.com"],
       "subject":"Hello","html":"<h1>It works</h1>"}'
```

Or with Python (`pip install seskit`):

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

## Engineering notes

Three changes that show how this project is maintained:

- [PR #7](https://github.com/Otitodev/seskit/pull/7): the API reference is generated from the OpenAPI schema, and CI runs the same script with `--check`, so the docs cannot drift from the routes.
- [PR #8](https://github.com/Otitodev/seskit/pull/8): `llms.txt` and `llms-full.txt` are generated at build time, so AI agents can read the docs without crawling the site.
- [PR #51](https://github.com/Otitodev/seskit/pull/51): a proof only PR (closed, never merged) that shows the new security tests fail against the old behaviour, so the tests are known to catch the bug.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues through
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
