# Errors

Every `/v1` failure leaves the building in one shape:

```json
{ "error": { "type": "domain_not_verified", "message": "..." } }
```

**Branch on `type`.** It is stable and domain-shaped. The `message` is written
for a human and may be reworded without warning.

The vocabulary is domain-shaped rather than HTTP-shaped on purpose:
`domain_not_verified` says what is wrong, where `BAD_REQUEST` only says that
something is.

## Types

| Type | Status | Means |
|---|---|---|
| `unauthorized` | 401 | Missing or invalid API key |
| `forbidden` | 403 | Authenticated, but not for this |
| `not_found` | 404 | No such resource in this project |
| `validation_error` | 422 | The request body is wrong; the message says how |
| `rate_limited` | 429 | Over the per-project limit; see the `X-RateLimit-*` headers |
| `domain_not_verified` | 400 | The `from` address is not covered by a verified identity |
| `aws_not_connected` | 400 | The project has no AWS connection and one is required |
| `provider_error` | 502 | SES refused or failed; the message is normalised, not raw |
| `internal_error` | 500 | Unexpected. Worth reporting |

## Raw provider errors never reach you

boto3 and AWS exception text is translated before it leaves SESKit. Passing it
through would leak account ids, ARNs and internal detail into a response your
own users might see, and would tie your error handling to AWS's wording.

## Which are worth retrying

| | |
|---|---|
| **Retry** | `rate_limited` after the reset, `provider_error`, `internal_error` |
| **Do not retry** | `unauthorized`, `forbidden`, `not_found`, `validation_error`, `domain_not_verified`, `aws_not_connected` |

The second group are configuration problems. Retrying them produces the same
answer more often — alert someone instead.

Use an `Idempotency-Key` on any send you might retry, so a retry after an
ambiguous failure cannot deliver a second copy.
