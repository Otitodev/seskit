# Events and payloads

The vocabulary SESKit normalises Amazon SES's notifications into, and the shape
it forwards to your [webhooks](../guides/webhooks.md).

## Event types

```text
email.sent   email.delivered   email.bounced      email.suppressed
email.opened email.clicked     email.complained
```

| Type | Emitted when |
|---|---|
| `email.sent` | A provider accepted the message |
| `email.delivered` | The receiving server accepted it |
| `email.bounced` | It could not be delivered |
| `email.complained` | The recipient marked it as spam |
| `email.opened` | The tracking pixel loaded — only with [tracking on](../guides/delivery-events.md#open-and-click-tracking) |
| `email.clicked` | A rewritten link was followed — tracking only |
| `email.suppressed` | SESKit added an address to the [suppression list](../guides/suppression.md) |

SES emits more types than these — `Reject`, `Rendering Failure`,
`DeliveryDelay` — which SESKit records but does not currently forward.

## Payload

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

| Field | |
|---|---|
| `id` | Unique per event. **Deduplicate on this** — delivery is at-least-once |
| `type` | One of the seven above |
| `email_id` | The message this is about; matches `GET /v1/emails/{id}` |
| `created_at` | When the event *happened*, not when SESKit heard about it |
| `data` | Type-dependent. Always includes `to` |

## Bounce types

The distinction that matters for your sender reputation:

| `bounce_type` | Means | Do |
|---|---|---|
| `Permanent` | The address does not exist, or refused permanently | **Stop sending to it.** These are what push your bounce rate up |
| `Transient` | A temporary failure — full mailbox, server down | Safe to retry later |
| `Undetermined` | The receiving server was unclear | Treat as transient, watch for repeats |

SESKit acts on this itself: a `Permanent` bounce puts the address on the
project's [suppression list](../guides/suppression.md) and later sends to it
are refused. `Transient` and `Undetermined` are left alone, because the address
usually works again tomorrow.

## `email.suppressed`

Raised by SESKit rather than by a provider, when an address is added to the
list.

```json
{
  "id": "evt_01J8XQ...",
  "type": "email.suppressed",
  "email_id": "email_01J8XQ...",
  "created_at": "2026-09-02T09:00:05+00:00",
  "data": {
    "to": ["user@example.com"],
    "reason": "bounce",
    "caused_by": "evt_01J8XP..."
  }
}
```

| Field | |
|---|---|
| `reason` | `bounce`, `complaint` or `unsubscribe` |
| `caused_by` | The event that led to it, or `null` — an unsubscribe is the recipient telling SESKit directly |

One event per cause, not per address: a bounce naming three dead mailboxes is
one thing that happened, so `to` is a list.

`created_at` is when SESKit suppressed the address, not when the bounce
occurred. A notification that sat in a queue for an hour did not suppress
anything an hour ago.

An address already on the list produces no second event, so a repeat bounce is
not something your application has to deduplicate.

## Timestamps

`created_at` is when the event occurred at the provider, not when SESKit
ingested it. A queue backlog that delivers a bounce an hour late still reports
the hour it happened, which is why the [metrics](../guides/metrics.md) filter
on it.
