# Your first email

Nothing here needs an AWS account. A project with no AWS connection sends
through Mailpit, so you can watch a real message travel the whole pipeline
before deciding whether SESKit is worth configuring.

This matters more than it sounds. A brand-new AWS account cannot send to
arbitrary recipients for roughly 24 hours — [leaving the SES
sandbox](../guides/ses-sandbox.md) is a support review. If first success
depended on that, most people would never get there.

## Three steps

1. Open <http://localhost:8000> and create the owner account. The first
   registration claims the instance; signup closes behind you.
2. Go to **API Keys**, create one, and copy it — **it is shown once**. SESKit
   stores a SHA-256 hash, so a lost key cannot be recovered, only replaced.
3. Send:

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "from": "hello@example.com",
    "to": ["you@example.com"],
    "subject": "Hello from SESKit",
    "html": "<h1>It works</h1>"
  }'
```

```json
{ "id": "email_01J8XQ...", "status": "queued" }
```

The message appears at <http://localhost:8025> within a second or two, and
under **Emails** in the dashboard with its status and provider message id.

## What `queued` means

It is literal. The request is validated and recorded synchronously, then the
send itself runs in the worker.

The split is deliberate: anything you could have got wrong — an unverified
sender, a malformed address, an oversized attachment — comes back immediately
as an error rather than surfacing in a log an hour later. What happens
afterwards is the provider's business, and that is what the
[delivery events](../guides/delivery-events.md) are for.

Recording before sending also means that if the process dies between accepting
your request and sending it, the record still exists and says `queued`, rather
than the request vanishing with nothing to show you.

## Sending the same thing twice

Repeat the request with an `Idempotency-Key` header and you get the same email
id back, and no second message.

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Idempotency-Key: welcome-user-42" \
  -H "Content-Type: application/json" \
  -d '{ "from": "hello@example.com", "to": ["you@example.com"],
        "subject": "Hello", "html": "<h1>Once</h1>" }'
```

The key is scoped to your project, so two customers on the same instance can
use the same string without colliding. A unique constraint adjudicates it
rather than a check-then-insert, which is what makes two simultaneous retries
of the same request safe.

## Where to go next

- [Sending from your app](sending-from-your-app.md) — the same call from your
  own code.
- [The dashboard](dashboard-tour.md) — what each page shows.
- [Connect an AWS account](../guides/connect-aws.md) — when you want the mail
  to actually leave.
