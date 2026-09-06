# seskit

The Python SDK for [SESKit](https://github.com/Otitodev/seskit) — a
Python-native, self-hosted developer email platform built on Amazon SES.

## What SESKit is

SESKit makes Amazon SES feel as simple as Resend, without giving up ownership
of your sending infrastructure. You run it yourself, on your own AWS account:
an HTTP API for sending, a dashboard for looking at what happened, delivery
event ingestion from SNS, and signed outbound webhooks.

**This package is the client, not the server.** You run the server; your
application installs this. The same distinction as a database and its driver.

## Install

```bash
pip install seskit
```

## Send

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_...", base_url="https://seskit.example.com")

sent = client.emails.send(
    from_="hello@example.com",
    to=["user@example.com"],
    subject="Welcome",
    html="<h1>Welcome!</h1>",
)
```

`base_url` is the address of your own instance. There is no default, because
there is no hosted SESKit to point at.

## Read

```python
email = client.emails.get(sent.id)
email.status          # queued, sending, sent, failed
email.delivered_at    # None until a delivery event arrives

page = client.emails.list(status="failed")
for email in page:
    print(email.id, email.last_error)
```

`list` returns one page, newest first. Pass `page.last_id` as `starting_after`
while `page.has_more`.

## Errors

Every refusal is a class, so you branch on the type rather than on the message:

```python
from seskit import SuppressedRecipient, DomainNotVerified, SESKitError

try:
    client.emails.send(...)
except SuppressedRecipient:
    ...          # the address hard-bounced or complained; it is on your list
except DomainNotVerified:
    ...          # the sender is not verified in SES
except SESKitError as error:
    ...          # anything else; error.type and error.message say what
```

## Retries

Rate limits, 5xx responses and connection failures are retried with backoff,
honouring `Retry-After`. Refusals are not — asking again produces the same
refusal.

Every send carries an `Idempotency-Key`, generated per call unless you pass
one, so a retry after a timeout cannot deliver a second copy. Pass your own
(an order id, say) when you have something that identifies the send across
process restarts.

## It is optional

The SDK is deliberately a thin client over the HTTP API. Business logic lives
in the API and is never duplicated here, so a Python call and a curl command
cannot disagree about what SESKit does.

The consequence is worth stating: this client can never do anything a `curl`
command cannot. Reaching for it is a convenience, never a requirement.

## Licence

MIT. See [LICENSE](LICENSE).
