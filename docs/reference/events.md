# Events and payloads

The vocabulary SESKit normalises Amazon SES's notifications into, and the shape
it forwards to your [webhooks](../guides/webhooks.md).

## Event types

```text
email.sent   email.delivered   email.bounced
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
| `type` | One of the six above |
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

!!! warning "Acting on hard bounces is currently your job"
    SESKit shows the rates but does not yet maintain a suppression list.
    Consume `email.bounced`, check `bounce_type`, and stop sending to permanent
    failures — AWS reviews accounts above 5% bounce.

## Timestamps

`created_at` is when the event occurred at the provider, not when SESKit
ingested it. A queue backlog that delivers a bounce an hour late still reports
the hour it happened, which is why the [metrics](../guides/metrics.md) filter
on it.
