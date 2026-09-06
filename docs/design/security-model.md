# Security model

What SESKit protects, how, and where the deliberate holes are.

## AWS credentials are never stored

SESKit resolves them from the environment the boto3 way and never writes them
anywhere. There is deliberately no configuration setting for them: naming one
would invite credentials into logs, config dumps and a database, and an IAM
role — the right answer in production — has nothing to name.

The `AWSConnection` record holds which credential source is active and the
account id, region and status it resolved to. It does not hold or broker
credentials.

## API keys are hashed; webhook secrets are not

| | Stored as | Why |
|---|---|---|
| **API key** | SHA-256 hash | SESKit only ever needs to recognise one, never reproduce it |
| **Webhook signing secret** | Plaintext | Your receiver has to read it back to verify signatures |

The asymmetry is deliberate and worth understanding, because it makes a
database backup a secret-bearing artifact. See
[backup and restore](../operating/backup.md).

There is no salted KDF on API keys, and that is also deliberate. A KDF makes
*low-entropy* secrets expensive to guess; a key is 256 random bits, so there is
no guessing to frustrate. Adding one would put latency on every authenticated
request and buy nothing.

A key is shown once at creation. A missing header and an invalid token get the
same 401 and the same message — distinguishing them would tell someone probing
which half they had right.

## Two untrusted boundaries

Both directions of the event pipeline face something SESKit does not control.

### Inbound: SNS notifications

The HTTPS receiver is unauthenticated by necessity — SNS has no credential to
present. So every request is verified against the RSA signature SNS signed it
with, using a certificate fetched only from `sns.<region>.amazonaws.com` and
only after that host is validated against a pattern anchored at both ends.

**Checking the topic ARN instead would not work.** It is a field in the request
body and topic ARNs are not secrets, so anyone who learns one could fabricate
bounce and complaint events — corrupting your metrics, and once suppression
exists, silently suppressing recipients they choose.

Notifications are deduplicated on the SNS message id with a unique constraint,
because SNS and SQS are both explicitly at-least-once and a double-counted
bounce inflates the rate AWS judges your account by.

### Outbound: webhook destinations

A user-supplied URL that SESKit will make requests to is a server-side request
forgery primitive, and because response bodies are captured into the delivery
log, it composes into a *read* primitive against your internal network.

So:

- Loopback, private and link-local ranges are refused outside local
  development.
- The check runs against the **resolved address**, not the string — otherwise
  DNS rebinding walks straight past it.
- It runs again at **every delivery**, not only at registration, because DNS
  can change afterwards.
- **Redirects are never followed.** A redirect would forward your signed
  payload to a host you never registered.
- Response capture is bounded to text-ish content types and a few kilobytes, so
  a hostile endpoint cannot stream gigabytes into your database.

`WEBHOOK_ALLOWED_CIDRS` is the deliberate hole. Only list ranges you genuinely
need, and remember that responses from them are recorded.

## Outbound signatures

SESKit signs what it sends you: HMAC-SHA256 over `"{timestamp}.{body}"`, sent
as `v1={hex}` with the timestamp in its own header.

The timestamp is **inside** the signed string, which is what makes a replay
detectable — signing the body alone would let a captured request be replayed
for ever with a fresh timestamp. The `v1=` prefix allows the scheme to change
later without a flag day.

[How to verify it](../guides/webhooks.md#verifying-the-signature).

## The dashboard

- **Sessions** are signed with `SECRET_KEY`, which refuses to boot on the
  example value.
- **CSRF tokens** on every state-changing form. Logging out is a form, not a
  link, so a prefetch cannot trigger it.
- **Message bodies are rendered as source, never as HTML.** The body is chosen
  by whoever holds an API key; rendering it in the account owner's
  authenticated session would be stored XSS with a session cookie attached.
- **Blind copies are stored but never displayed** beside other recipients. A
  blind copy that shows up in the interface is not blind.
- **Pages carry `Cache-Control: no-store`** when signed in, so the back button
  after a logout cannot re-display the previous user's dashboard from the
  browser cache.

## Response headers

Set by one middleware on every response, because the failure mode of
per-route security is a route somebody forgot.

| Header | |
|---|---|
| `Content-Security-Policy` | `default-src 'self'`, with a per-request nonce for the two inline theme scripts. No `unsafe-inline`, no `unsafe-eval` |
| `X-Frame-Options` / `frame-ancestors` | `DENY` / `'none'`. Every destructive action on the dashboard is a one-click form, which is what clickjacking needs |
| `form-action 'self'` | The directive framing protection has no answer for: injected markup posting a CSRF token to another origin |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `same-origin`, so an unsubscribe token or an email id in a path does not leave with the `Referer` |
| `Strict-Transport-Security` | Outside local development only |

**A strict policy is affordable because of a decision made in Phase 1.** §5
forbids Node, npm and a build step, so every script and stylesheet is a local
file: no CDN, no analytics, no embedded fonts, no `data:` URIs. `default-src
'self'` costs a codebase built that way nothing, and would be expensive to
adopt later.

The autoescaping above is the defence against injected markup. The policy is
the layer that holds on the day an escape is missed, and a test walks the
template tree and fails on any inline script without a nonce — a missing nonce
does not raise, it just silently refuses to run.

HSTS is not set locally on purpose. A browser that has seen it refuses every
other project served from `http://localhost`, and the only cure is clearing
HSTS state by hand.

## Request size

`MAX_REQUEST_BYTES` caps a request body **before anything reads it**. A
declared `Content-Length` is refused without reading a byte; a chunked body is
counted as it arrives, because `Content-Length` is a header the client controls
and refusing on it alone is a courtesy rather than a defence.

Distinct from `EMAIL_MAX_MESSAGE_BYTES`, which is checked against the assembled
message *after* the body has been read, parsed and base64-decoded into memory.
The first protects the server; the second protects the send.

## Tenancy

Ownership is part of every query rather than a check after it. An id belonging
to another project resolves to nothing — the same answer as "no such record" —
so a stranger cannot probe for real ids by watching which ones return a
different error.

## Logging

Structured logs record ids: which key, which email, which endpoint. They do not
record message bodies, recipients or secrets. A `repr` of an email deliberately
omits its subject and addresses, because reprs end up in logs.

## What is not covered yet

Stated plainly rather than left to be discovered:

- **No HTTP API for the suppression list.** Addresses are suppressed
  automatically and managed on the dashboard; there is no `/v1` endpoint to
  read or bulk-load the list. See [suppression](../guides/suppression.md).
- **No RBAC.** An account owns its projects; there are no roles or team
  members.
- **No per-IP rate limit.** Limits are per project for the API and per account
  for sign-in; an unauthenticated flood is a job for whatever sits in front of
  this.
- **Suppression removal is not audited.** The row records who suppressed an
  address and when, but not who took it off the list.
- **Migrations are not audited for backward compatibility**, so rolling
  upgrades are not a supported story. See
  [upgrading](../operating/upgrading.md).

Security issues should go to
[SECURITY.md](https://github.com/Otitodev/seskit/blob/main/SECURITY.md) rather
than a public issue.
