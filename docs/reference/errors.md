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
| `invalid_request` | 400 | The request cannot be acted on. A body that fails schema validation is the same type at **422** |
| `authentication_failed` | 401 | Missing, malformed, unknown or revoked API key — one answer for all four, so a caller cannot learn which guesses were closer |
| `authorization_failed` | 403 | Authenticated, but not for this |
| `not_found` | 404 | No such resource in this project |
| `domain_not_verified` | 422 | The `from` address is not covered by a verified identity |
| `invalid_recipient` | 422 | An address in `to`, `cc` or `bcc` is not an email address |
| `suppressed_recipient` | 422 | An address is on the project's [suppression list](../guides/suppression.md) |
| `email_rejected` | 422 | SES refused the message itself |
| `attachment_too_large` | 413 | The assembled message is over the limit. Attachments grow by about a third once encoded |
| `rate_limit_exceeded` | 429 | Over SESKit's per-project limit; see the `X-RateLimit-*` headers and `Retry-After` |
| `sending_limit_exceeded` | 429 | Over the SES account's own quota, which SESKit cannot raise |
| `provider_error` | 502 | SES refused or failed; the message is normalised, not raw |
| `internal_error` | 500 | Unexpected. Worth reporting |

## Raw provider errors never reach you

boto3 and AWS exception text is translated before it leaves SESKit. Passing it
through would leak account ids, ARNs and internal detail into a response your
own users might see, and would tie your error handling to AWS's wording.

For the same reason a validation message names the fields that were wrong and
never the values that were submitted — echoing those back would put whatever
was posted into your logs.

## Which are worth retrying

| | |
|---|---|
| **Retry** | `rate_limit_exceeded` after the reset, `sending_limit_exceeded` after the window, `provider_error`, `internal_error` |
| **Do not retry** | `invalid_request`, `authentication_failed`, `authorization_failed`, `not_found`, `domain_not_verified`, `invalid_recipient`, `suppressed_recipient`, `email_rejected`, `attachment_too_large` |

The second group are configuration or content problems. Retrying them produces
the same answer more often — alert someone instead.

`suppressed_recipient` is the one in that list a person can clear: the address
is on your own list, and the [Suppressions page](../guides/suppression.md) can
take it off.

Use an `Idempotency-Key` on any send you might retry, so a retry after an
ambiguous failure cannot deliver a second copy.
