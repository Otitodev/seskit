# Python SDK

A thin client over the [HTTP API](api.md).

```bash
pip install seskit
```

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

`base_url` is the address of **your** instance and has no default. There is no
hosted SESKit to point at, and a default of `localhost` would turn a forgotten
argument into mail that silently goes nowhere.

`from_` has the trailing underscore because `from` is a Python keyword. It goes
out over the wire as `from`.

## The three calls

```python
sent = client.emails.send(from_=..., to=..., subject=..., html=...)
email = client.emails.get(sent.id)
page = client.emails.list(status="failed")
```

### `send`

Returns as soon as SESKit accepts the message, which is before it is sent — the
send happens in a worker. So `sent.status` is `queued`, and `get` is how you
find out where it got to.

| Argument | |
|---|---|
| `from_`, `to`, `subject` | Required. `to` takes one address or a list |
| `html`, `text` | At least one. Both makes a multipart message |
| `cc`, `bcc`, `reply_to` | One address or a list |
| `headers` | `dict[str, str]` of custom headers |
| `attachments` | `(filename, bytes, content_type)` tuples |
| `idempotency_key` | Yours, if you have one. Generated otherwise |

```python
client.emails.send(
    from_="hello@example.com",
    to="user@example.com",
    subject="Your report",
    text="Attached.",
    attachments=[("report.csv", b"a,b\n1,2\n", "text/csv")],
)
```

JSON has no byte type, so the client base64-encodes attachment content. That is
the one transformation it makes — see
[what it will not do](#what-it-deliberately-does-not-do).

### `get`

```python
email = client.emails.get("email_01J8XQ...")
email.status  # queued, sending, sent, failed
email.delivered_at  # None until a delivery event arrives
email.last_error  # why the last attempt failed, if it did
```

There is no `bcc`. The API does not return one — a blind copy readable from a
`GET` is not blind — so there is nothing here to hold it.

A field this version of the client does not know about is still available in
`email.raw`, rather than being silently dropped.

### `list`

```python
page = client.emails.list(limit=50, status="failed")
for email in page:
    print(email.id, email.last_error)
```

**One page**, newest first. Iterating gives the messages on that page and
deliberately does not fetch more — a loop that silently made more requests
would turn one line into an unbounded number of them against your own rate
limit.

```python
cursor = None
while True:
    page = client.emails.list(limit=100, starting_after=cursor)
    handle(page.data)
    if not page.has_more:
        break
    cursor = page.last_id
```

## Async

The same surface, awaited:

```python
from seskit import AsyncSesKit

client = AsyncSesKit(api_key="sk_live_...", base_url="https://seskit.example.com")


async def welcome(address: str) -> str:
    sent = await client.emails.send(
        from_="hello@example.com",
        to=[address],
        subject="Welcome",
        html="<h1>Welcome!</h1>",
    )
    return sent.id
```

It exists because the reader most likely to install this is writing a FastAPI
or Starlette handler, and a blocking HTTP call inside one stops the event loop
for the length of the request — every other request on that worker waits behind
a call to a mail server.

Both clients build their requests with the same code, so they cannot drift into
disagreeing about what a send looks like.

## Errors

Every refusal is a class, so you branch on the type rather than on the message:

<!-- docs-test: illustrative -->
```python
from seskit import SuppressedRecipient, DomainNotVerified, SESKitError

try:
    client.emails.send(from_=..., to=..., subject=..., html=...)
except SuppressedRecipient:
    ...  # the address hard-bounced or complained; it is on your list
except DomainNotVerified:
    ...  # the sender is not verified in SES
except SESKitError as error:
    print(error.type, error.status_code, error.message)
```

One class per [error type](errors.md), all inheriting `SESKitError`. An error
type newer than your copy of the client still raises `SESKitError`, with
`error.type` carrying the name — a client that is behind the server refuses
usefully rather than raising a `KeyError` from inside itself.

`SESKitConnectionError` is the exception to the pattern: the request never got
an answer at all. A send that fails that way may or may not have been accepted,
which is what the idempotency key below is for.

## Retries and idempotency

Rate limits, `5xx` responses and connection failures are retried with
exponential backoff, honouring `Retry-After` when the server sends one.
Refusals are never retried — asking again produces the same refusal.

**Every send carries an `Idempotency-Key`**, generated per call unless you pass
one. Without it, a retry after a timeout could deliver a second copy of a
message SESKit had already accepted, and a timeout is exactly the case the
retry exists for.

Pass your own when your application already has something that identifies the
send — an order id deduplicates across process restarts, which a key generated
per call cannot.

```python
client.emails.send(..., idempotency_key=f"order-{order.id}")
```

## Configuration

| Argument | Default | |
|---|---|---|
| `api_key` | — | Required |
| `base_url` | — | Required. Your instance |
| `timeout` | `30.0` | Seconds, per attempt |
| `max_attempts` | `3` | One try and two retries |
| `http_client` | — | Your own `httpx.Client`, if you need one |

## What it deliberately does not do

Business logic lives in the API and is never duplicated here. That is a
constraint from [§13](https://github.com/Otitodev/seskit/blob/main/SESKit_MVP.md),
and it has a consequence worth stating plainly: **the SDK can never do anything
a `curl` command cannot.**

So it does not validate addresses, does not check the suppression list, and
does not decide what a failure meant — only whether asking again could change
the answer. An address the client rejected would be one the API never got to
have an opinion about, and the two would then disagree with the client winning
by default.

So reaching for it is a convenience — typed responses, less boilerplate, errors
you can catch — and never a requirement. If your language is not Python, or you
would rather not add a dependency, an HTTP request is a first-class way to use
SESKit rather than a fallback.

## The two halves, again

`pip install seskit` goes in **your application**. It does not install SESKit
itself — that is the repository, running as a server. See
[the home page](../index.md) if that distinction is new.
