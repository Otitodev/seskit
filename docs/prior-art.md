# Prior art

Notes from reading comparable open-source projects, and the requirements that
reading generated. Kept because the reasoning behind a requirement is worth more
than the requirement, and because the licence boundary below needs to be visible
to anyone who works on this.

## The licence boundary

**useSend** (`github.com/usesend/usesend`) and **Plunk**
(`github.com/useplunk/plunk`) are both **AGPL-3.0**. SESKit is MIT (§28).

**No code from either may enter this repository.** AGPL is strongly copyleft;
incorporating any of it would relicense SESKit and destroy the "adopt it freely"
positioning that the MIT choice exists to protect.

Ideas, architecture and API shape are not copyrightable, and everything below is
recorded as design reasoning. Where a later phase implements one of these, write
it from the AWS documentation rather than from a memory of their source.

Reviewed 2026-08-28 against useSend at that date.

## Where we independently agreed

Reassuring rather than actionable - the closest comparable project reached the
same conclusions from the same constraints.

- AWS credentials resolved from the environment, falling back to the SDK default
  chain. Never stored in the database (§9).
- `sts:GetCallerIdentity` + `sesv2:GetAccount` as the account check.
- Domain status vocabulary: `NOT_STARTED / PENDING / SUCCESS / FAILED /
  TEMPORARY_FAILURE`. Both took the SES field values.
- An `apps/` + `packages/` monorepo split.

## Adopted (see `31cbb35`)

**Redis failures should degrade, not fail.** Their rate limiter catches Redis
errors and continues; ours propagated, so a Redis blip returned 500 for every
`/v1` request. Worse on the key path: a cache outage became an authentication
outage even though the database held the answer.

The directions are deliberately opposite - the limiter fails **open**,
authentication fails **closed**. A limiter guards a working service; refusing
every request because the counter is unavailable is a worse failure than briefly
not enforcing a quota. Authentication has no such argument.

## Where SESKit was already ahead

Recorded so these are not "simplified" later by someone who assumes the
mainstream approach is better.

| | useSend | SESKit |
|---|---|---|
| `last_used` write | every request, fire-and-forget, with a TODO to queue it | Redis `SET NX` marker, once per interval |
| Key verification | database hit **plus scrypt** per request | Redis cache, invalidated on revoke |
| Rejection message | distinguishes missing header from invalid token | one uniform message, asserted by test |
| Invalid key status | 403 | 401 |
| Error vocabulary | HTTP-shaped (`BAD_REQUEST`) | domain-shaped per §19 (`domain_not_verified`) |
| Limiter | `INCR` + `EXPIRE` + `TTL`, and the key never expires if the process dies between the first two | `INCR` + `EXPIRE NX` in one pipeline |
| SES sandbox | a warning in the self-hosting docs | detected and surfaced in the product |

### On API key hashing

Their key is `us_{clientId}_{token}`: look up by an indexed `clientId`, then
verify the token with salted scrypt.

That defeats one of the arguments originally written into
`security/api_keys.py` - that a salted KDF is impossible because a key cannot be
looked up by its hash. With a separate lookup id, it can.

The conclusion did not change but the reasoning did, and the docstring was
corrected. A KDF makes *low-entropy* secrets expensive to guess; there is no
guessing to frustrate against 256 random bits, so scrypt would add latency to
every authenticated request and buy nothing. Their implementation also uses
`scryptSync`, which blocks the event loop per request.

## Requirements this generates

### Phase 5 - domains

- **Two-tier recheck backoff.** An hourly job, but each domain has its own due
  check: unverified rechecked every ~6h, verified every ~30 days. The long
  recheck is the subtle half - it catches a domain whose DNS records were removed
  *after* verification, which otherwise looks healthy until a send fails.
- **Custom MAIL FROM** via `PutEmailIdentityMailFromAttributes`, which is what
  populates the `mail_from_status` column in §6.
- **A test-send control on the domain page**, where the user already is after
  verifying.

### Phase 7 - event ingestion

Their SNS handling is the weakest part of their codebase. Four requirements come
directly from what it does not do:

1. **Verify the SNS signature.** Their only check is that `TopicArn` matches a
   configured value - but `TopicArn` is a field in the request body, and topic
   ARNs are not secrets. Anyone who learns one can fabricate Bounce and Complaint
   events: corrupting analytics, and if bounces ever feed a suppression list,
   silently suppressing arbitrary recipients.
2. **Validate `SubscribeURL` before fetching it.** They fetch it unvalidated on
   `SubscriptionConfirmation`, which is a server-side request forgery vector. The
   host must be `sns.<region>.amazonaws.com`.
3. **Deduplicate on the SNS `MessageId`.** SNS is explicitly at-least-once. They
   thread `messageId` into the queue but pass it as the job *name*, which is not
   unique, so no deduplication happens and redelivery double-counts events. Use a
   real unique constraint or a Redis marker, and test it by delivering the same
   id twice.
4. **Return non-2xx when SNS should retry.** They return 200 on parse failures,
   so a transient bug drops events permanently and silently.

Accept-and-queue is right, though: parse only enough to validate, enqueue, and
return. SNS has a delivery timeout and a slow handler causes retries.

Worth copying too: a flag recording that the subscription was confirmed, so the
dashboard can answer "why am I seeing no events?".

SES event types (§15): `Send`, `Delivery`, `Bounce`, `Complaint`, `Reject`,
`Open`, `Click`, `Rendering Failure`, `DeliveryDelay`.

### Phase 8 - outbound webhooks

Their best-built subsystem, and the asymmetry with their SNS handling is itself
the lesson: outbound webhooks were a marquee feature and got care, inbound SNS
was plumbing and got none. Both are untrusted boundaries.

- **Signature:** `HMAC-SHA256(secret, "{timestamp}.{body}")`, sent as `v1={hex}`
  with the timestamp in its own header. The timestamp must be *inside* the signed
  payload, or an attacker replays the same body with a fresh one. The `v1=`
  prefix allows rotating the scheme later.
- **`redirect: manual`.** Never follow redirects - a redirect forwards the signed
  payload to a host the user never registered.
- **Bounded response capture:** only text-ish content types, only a few KB.
  Otherwise a hostile endpoint streams gigabytes into the delivery log.
- **Exponential backoff with jitter** (theirs: 5s x 2^(n-1), 30% jitter, 6
  attempts). The jitter matters when many endpoints share a host - without it
  they all retry in lockstep.
- **Auto-disable after N consecutive failures**, as a status distinct from
  user-disabled, so the UI can explain why and offer re-enable.
- **Validate the destination URL** - the thing they are missing. A user can
  register an internal address, and because the response body is captured and
  shown back in the dashboard, that composes into a read primitive against the
  internal network. Reject loopback, private and link-local ranges, require HTTPS
  outside local, and check the *resolved* address rather than the string, or DNS
  rebinding walks past the check.

## Onboarding friction

A brand-new AWS account cannot send to arbitrary recipients for ~24 hours -
sandbox exit is a Support review. No amount of interface work removes that, so
first success must not depend on it.

The ladder, each rung a real success:

0. **Mailpit, 60 seconds, no AWS.** The `SMTPProvider` (§25/§26). This is the
   anti-defection mechanism, not a developer convenience.
1. **A verified email-address identity, ~5 minutes, no DNS.** SES emails a link;
   clicking it allows real sending within the sandbox. The mailbox simulator
   addresses need no verification at all.
2. **A verified domain**, minutes to 72h depending on the DNS provider.
3. **Production access**, ~24h.

`sesv2:PutAccountDetails` submits the production-access request over the API, so
step 3 can be a form inside SESKit rather than a trip to the console. useSend
handles this as a line in its documentation, which is exactly how users end up
confused about why mail is not arriving.

Neither useSend nor Plunk has an onboarding flow, and neither supports
email-address identities. The friction ladder is a differentiator, not table
stakes.
