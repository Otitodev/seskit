# Webhooks

[Delivery events](delivery-events.md) tell SESKit what happened to a message.
Webhooks tell **your application**, so it can suppress a bounced address or
flag a complaint without polling `GET /v1/emails/{id}` for everything it has
ever sent.

Add an endpoint on the **Webhooks** page. SESKit then POSTs a signed JSON body
for each of six event types:

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

!!! important "Webhooks need delivery events set up first"
    Without a configuration set, Amazon SES publishes nothing, so there is
    nothing to forward. See [delivery events](delivery-events.md).

## Verifying the signature

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
import hashlib
import hmac
import time


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

## Retries and failure

| | |
|---|---|
| **Retried** | 5xx, 429, timeouts, connection failures |
| **Not retried** | 4xx — the endpoint understood and refused |
| **Backoff** | `5s × 2ⁿ` with 30% jitter, six attempts, ≈5 minutes total |
| **Auto-disable** | 10 consecutive failures, reset by any success |

The jitter matters when many endpoints share a host: without it they all retry
in lockstep and arrive as a thundering herd.

An endpoint SESKit switches off gets a status of its own — the page says it
stopped and why, rather than showing a switch that appears to have moved by
itself. Re-enabling clears the failure count.

Deliveries are **at-least-once**: the same event can arrive twice if your
endpoint is slow to answer. Deduplicate on the event `id`.

## Changing an endpoint's URL

Edit it on the Webhooks page. Three things worth knowing:

- **The signing secret does not change.** Moving your receiver should not
  require re-keying verification on both sides at the same moment.
- **Deliveries already queued go to the new URL**, because the worker reads the
  endpoint at attempt time. Your receiver moved; its backlog moves with it.
- **The failure count resets, but a paused endpoint stays paused.** The count
  described an address that is now gone. Resuming outbound requests is a larger
  act than editing a field, so it stays an explicit click.

The new URL is checked against the same rules as a new registration.

## Where SESKit will and will not send

!!! danger "Endpoint URLs are validated against SSRF"
    At registration **and again at every delivery**, against the resolved
    address rather than the string — otherwise DNS rebinding walks straight
    past the check.

    Loopback, private and link-local addresses are refused outside local
    development, and **redirects are never followed**: a redirect would forward
    your signed payload to a host you never registered.

    If you genuinely need an internal destination in production, list the range
    in `WEBHOOK_ALLOWED_CIDRS`.

Response bodies are captured for the delivery log, but only text-ish content
types and only a few kilobytes — otherwise a hostile endpoint could stream
gigabytes into your database.
