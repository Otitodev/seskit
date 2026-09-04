# Sending from your app

By now you have a SESKit instance running somewhere. This page is about the
other half: calling it from the application that actually wants to send mail.

```text
your application                   your SESKit server
┌─────────────────────┐           ┌────────────────────────┐
│  POST /v1/emails    │  ─HTTP→   │ API → queue → worker   │ ──→ SES or Mailpit
│  Bearer sk_...      │           │                        │
└─────────────────────┘           └────────────────────────┘
```

Usually a different machine, often a different codebase. Nothing about your
application needs to know SESKit exists beyond a base URL and an API key.

!!! warning "There is no SDK yet"
    The `seskit` package on PyPI reserves the name and contains no working
    client. Call the HTTP API directly — that is the supported path, and it is
    what every example here uses. See [Python SDK](../reference/sdk.md).

## Python

No dependency beyond an HTTP client you almost certainly already have.

```python
import httpx

SESKIT_URL = "https://seskit.internal.example.com"
SESKIT_KEY = "sk_live_..."


def send_welcome(address: str) -> str:
    response = httpx.post(
        f"{SESKIT_URL}/v1/emails",
        headers={"Authorization": f"Bearer {SESKIT_KEY}"},
        json={
            "from": "hello@example.com",
            "to": [address],
            "subject": "Welcome to Acme",
            "html": "<h1>Welcome!</h1>",
            "text": "Welcome!",
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["id"])
```

Send `text` alongside `html` whenever you can. Some clients prefer it, some
recipients insist on it, and a message with only an HTML part is more likely to
be scored as spam.

## Making retries safe

If your own job runner can retry — and it can — send an `Idempotency-Key`
derived from what the message *is*, not from when it was sent:

```python
headers = {
    "Authorization": f"Bearer {SESKIT_KEY}",
    "Idempotency-Key": f"welcome:{user.id}",
}
```

A retry then returns the original email id instead of sending a second copy. A
key containing a timestamp or a random value defeats the entire point, because
the retry generates a different one.

## Handling errors

Failures come back in one shape:

```json
{ "error": { "type": "domain_not_verified", "message": "..." } }
```

The `type` is stable and worth branching on; the `message` is for humans and
may be reworded. The full list is in [Errors](../reference/errors.md).

```python
if response.status_code >= 400:
    error = response.json()["error"]
    if error["type"] == "domain_not_verified":
        ...  # a configuration problem: alert someone, do not retry
    raise RuntimeError(error["message"])
```

## Rate limits

Limits are **per project, not per key**, and every response carries them:

```text
X-RateLimit-Limit       100
X-RateLimit-Remaining   87
X-RateLimit-Reset       1756800060
```

Issuing a second API key does not buy more headroom — see
[Configuration](../reference/configuration.md) for
`API_RATE_LIMIT_PER_MINUTE`.

## Knowing what happened next

The response tells you SESKit accepted the message. It cannot tell you the mail
arrived, because at that moment nobody knows.

- Poll `GET /v1/emails/{id}` for one message.
- Or, far better, have SESKit tell you: [webhooks](../guides/webhooks.md) push
  a signed event when a message is delivered, bounces, or is reported as spam.

Polling every message you have ever sent is the thing webhooks exist to stop
you doing.
