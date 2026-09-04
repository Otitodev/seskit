# HTTP API

Authenticate with `Authorization: Bearer sk_...`. A running instance serves
interactive docs at `/docs` and the OpenAPI schema at `/openapi.json`.

| Method | Path | |
|---|---|---|
| `POST` | `/v1/emails` | Send an email. Accepts `Idempotency-Key` |
| `GET` | `/v1/emails/{email_id}` | Retrieve one email and its status |
| `GET` | `/v1/domains` | List sending identities and their verification state |
| `GET` | `/v1/api-keys` | List this project's keys (never the secrets) |
| `GET` | `/v1/webhooks` | List webhook endpoints (never the signing secrets) |
| `GET` | `/v1/webhooks/{endpoint_id}/deliveries` | Recent delivery attempts and their status |
| `POST` | `/v1/events/ses` | SNS notification receiver. Not for callers — see [delivery events](../guides/delivery-events.md) |

The `/v1` surface is deliberately **read-only apart from sending**. Creating
identities, keys and webhook endpoints happens in the dashboard, where the
consequences can be explained before the button is pressed.

## Authentication

Keys are scoped to a project and carried as a bearer token.

```bash
curl http://localhost:8000/v1/emails/email_01J8XQ... \
  -H "Authorization: Bearer sk_..."
```

A key is shown once at creation and stored as a SHA-256 hash, so a lost key is
replaced rather than recovered. Revoking one takes effect immediately.

A missing header and an invalid token get the same `401` and the same message.
Distinguishing them would tell someone probing which half they had right.

## Sending

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "from": "hello@example.com",
    "to": ["user@example.com"],
    "cc": [],
    "bcc": [],
    "reply_to": ["support@example.com"],
    "subject": "Welcome",
    "html": "<h1>Hello</h1>",
    "text": "Hello"
  }'
```

| Field | | |
|---|---|---|
| `from` | required | Must be covered by a [verified identity](../guides/verify-a-sender.md) once AWS is connected |
| `to` | required | At least one address |
| `cc`, `bcc`, `reply_to` | optional | Lists of addresses |
| `subject` | required | |
| `html`, `text` | at least one | Send both where you can |
| `attachments` | optional | `filename`, base64 `content`, `content_type` |

Returns `201` with the email id and a status of `queued`. See
[your first email](../getting-started/first-email.md) for what `queued` means
and how `Idempotency-Key` behaves.

## Errors

One shape, always:

```json
{ "error": { "type": "domain_not_verified", "message": "..." } }
```

Branch on `type`; show `message` to humans. The list is in
[Errors](errors.md).

## Rate limits

Per project, not per key. Every response carries:

```text
X-RateLimit-Limit       100
X-RateLimit-Remaining   87
X-RateLimit-Reset       1756800060
```

Exceeding the limit returns `429`. Configure with
`API_RATE_LIMIT_PER_MINUTE`.
