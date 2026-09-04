# Delivery events

`sent` means Amazon SES accepted the message. That is the last thing SESKit can
observe on its own — whether it *arrived*, bounced, or was reported as spam is
knowledge that lives at AWS, and **SES does not report it anywhere unless
asked**.

## Setting it up

Press **Set up event reporting** on the AWS page. SESKit creates, in the region
the project is connected to:

| Resource | Purpose |
|---|---|
| SQS queue `seskit-events` | the worker polls this |
| SNS topic `seskit-events` | SES publishes here; it fans out to the queue |
| SES configuration set `seskit` | sends name it, or SES publishes nothing |

Messages sent from that point on show a delivery history: delivered, bounced
with the reason SES gave, marked as spam.

This is the one action in SESKit that creates resources in your own AWS
account, which is why the page names them before you press the button and why
disconnecting removes them again. It needs the second
[IAM policy](iam-policies.md).

!!! warning "Messages sent before setup gain nothing"
    SES only reports on mail sent through a configuration set, so existing
    messages have no history and never will. The message page distinguishes
    that from "no events yet" rather than showing an ambiguous blank.

## Two ways events get back

Set by `EVENT_INGESTION`:

| Mode | How | When to use |
|---|---|---|
| **`sqs`** *(default)* | The worker polls the queue | Anywhere. No inbound port, no public hostname, no certificate — AWS cannot POST to a laptop |
| **`https`** | SNS posts to `POST /v1/events/ses` | A public address with TLS. Lower latency, no polling. Needs `PUBLIC_BASE_URL` |
| **`both`** | Both at once | Migrating between the two |

SQS is the default because it works from anywhere, including a laptop behind
NAT with no certificate. HTTPS is lower latency but requires SESKit to be
reachable from the internet.

### How the HTTPS endpoint is protected

It is unauthenticated by necessity: SNS has no credential to present.

Every request is verified against the RSA signature SNS signed it with, using a
certificate fetched **only** from `sns.<region>.amazonaws.com`, and only after
that host is validated. An unsigned or altered notification is refused with 403
and nothing is recorded. The endpoint is not mounted at all unless
`EVENT_INGESTION` asks for it.

Checking only the topic ARN — which is a field in the request body, and not a
secret — would let anyone who learns one fabricate bounce and complaint events.
See the [security model](../design/security-model.md).

## Open and click tracking

Off unless you turn it on, per project, and worth understanding before you do.

!!! danger "This changes the mail your recipients receive"
    Enabling it asks Amazon SES to **rewrite every link in the mail this
    project sends** so clicks route through an Amazon-operated domain, and to
    add an invisible tracking pixel to HTML messages.

    Your recipients see the rewritten links. That is a visible change to your
    own product with privacy consequences, which is why it is a deliberate
    choice rather than a default.

With tracking off, open and click rates read **"Not tracked"** rather than
`0%` — see [reading your metrics](metrics.md).

## Duplicate events

SNS and SQS are both explicitly at-least-once, so the same notification will
arrive twice sooner or later.

SESKit deduplicates on the SNS message id with a unique constraint, so a
redelivered bounce is recorded once. This matters more than it sounds: a
double-counted bounce inflates the rate AWS judges your account by.

## Removing it

Removing event reporting, or disconnecting the account, deletes what was
created. Nothing else in your account is touched, and if a second project on
the same instance shares the region, the infrastructure stays until the last
one stops using it.

## Getting events into your own application

Delivery events tell *SESKit* what happened. To tell **your application**, set
up [webhooks](webhooks.md).
