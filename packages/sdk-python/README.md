# seskit

The Python SDK for [SESKit](https://github.com/Otitodev/seskit) — a
Python-native, self-hosted developer email platform built on Amazon SES.

> **Status: placeholder.** This release reserves the name and carries no
> working client yet. The SDK is built in Phase 10; until then, talk to the
> HTTP API directly. Watch the repository for the first functional release.

## What SESKit is

SESKit makes Amazon SES feel as simple as Resend, without giving up ownership
of your sending infrastructure. You run it yourself, on your own AWS account:
an HTTP API for sending, a dashboard for looking at what happened, delivery
event ingestion from SNS, and signed outbound webhooks.

## The intended surface

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_...", base_url="https://seskit.example.com")

client.emails.send(
    to=["user@example.com"],
    from_="hello@example.com",
    subject="Welcome",
    html="<h1>Welcome!</h1>",
)
```

The SDK is deliberately a thin client over the HTTP API. Business logic lives
in the API and is never duplicated here, so a Python client and a curl command
cannot disagree about what SESKit does.

## Licence

MIT. See [LICENSE](LICENSE).
