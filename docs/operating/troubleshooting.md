# Troubleshooting

Ordered by how often each one turns out to be the answer.

## Mail is stuck at `queued`

**The worker is not running.** Sending is queued, so the API accepts a message
and something else has to send it.

```bash
docker compose ps               # is the worker up?
docker compose logs -f worker
```

If the worker is running but nothing moves, check Redis — the queue lives
there, and a worker that cannot reach it says so once on startup and then sits
quietly.

## A send fails with an unverified sender

The `from` address is not covered by a verified identity, and the project has
AWS connected. SESKit refuses rather than falling back to Mailpit, because
falling back would report success while the message reached nobody.

Add the address or its domain on the **Domains** page — see
[verify a sender](../guides/verify-a-sender.md).

## A send fails with a rejected recipient

Almost always the [SES sandbox](../guides/ses-sandbox.md): inside it you can
only send to verified addresses. The AWS page says whether you are still in it.

## No delivery events are arriving

In order of likelihood:

1. **Event reporting was never set up.** It is a separate button on the AWS
   page, and it needs the second [IAM policy](../guides/iam-policies.md).
2. **The messages predate the setup.** SES only reports on mail sent through a
   configuration set, so older messages have no history and never will.
3. **The worker is not polling.** Check its logs for SQS errors — usually a
   missing `sqs:ReceiveMessage` permission.
4. **You are on HTTPS ingestion and SNS cannot reach you.** Confirm
   `PUBLIC_BASE_URL` is correct, publicly resolvable and holds a valid
   certificate. A subscription stuck at `PendingConfirmation` means SNS never
   got an answer.

## Webhooks are not being delivered

The delivery history on the **Webhooks** page records every attempt with its
status and response.

| What you see | Means |
|---|---|
| 4xx responses | Your endpoint refused. Not retried — SESKit reads a 4xx as a decision |
| Timeouts | Your endpoint is too slow. Answer immediately, process afterwards |
| "SESKit stopped sending" | Ten consecutive failures disabled it. Fix the endpoint, then re-enable |
| Nothing at all | There are no events to forward. See the section above |

A destination refused at registration is being blocked as
[SSRF](../guides/webhooks.md#where-seskit-will-and-will-not-send) — loopback
and private addresses are rejected outside local development.

## Signature verification fails at my endpoint

Nearly always one of three, in order:

1. **You are verifying a re-serialised body.** Parse *after* verifying, and use
   the raw bytes.
2. **You left the timestamp out of the signed string.** It is
   `"{timestamp}.{body}"`, not the body alone.
3. **The secret belongs to a different endpoint.** Each one has its own.

See [verifying the signature](../guides/webhooks.md#verifying-the-signature).

## The dashboard shows dashes instead of rates

That is correct. `—` means the denominator is zero: nothing has been sent yet
in the selected range, and `0%` would assert something untrue.

If open and click read **"Not tracked"**, tracking is off for that project,
which is the default. See [reading your metrics](../guides/metrics.md).

## It will not start

| Symptom | Cause |
|---|---|
| Refuses to boot, complains about `SECRET_KEY` | Still set to the example value |
| Cannot reach the database | Check `DATABASE_URL`; on a laptop check the port really is 55432 |
| Connects, but the data is wrong | Something else is bound to that port. This is why the ports are unusual — see [install](../getting-started/installation.md) |
| Migrations fail | Compare `uv run alembic current` against `alembic history` |

## Reading the logs

Logs are structured. Every send, event and webhook delivery is recorded with
ids, and **never with message bodies or recipients** — those stay in the
database rather than being scattered through log files.

```bash
docker compose logs -f api worker
```
