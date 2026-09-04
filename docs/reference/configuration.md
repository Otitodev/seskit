# Configuration

Everything comes from the environment, or a local `.env`. The annotated full
list lives in
[`.env.example`](https://github.com/Otitodev/seskit/blob/main/.env.example).

| Variable | Default | |
|---|---|---|
| `SECRET_KEY` | *required* | Signs sessions. Refuses to boot on the placeholder value |
| `DATABASE_URL` | *required* | PostgreSQL, async driver |
| `REDIS_URL` | *required* | Caching, rate limits, the job queue |
| `ALLOW_SIGNUP` | `false` | Registration always opens while no account exists, then closes |
| `AWS_DEFAULT_REGION` | `us-east-1` | Pre-selects the region in the connect form |
| `EVENT_INGESTION` | `sqs` | `sqs` · `https` · `both` |
| `EVENT_RESOURCE_PREFIX` | `seskit` | Names the SQS queue and SNS topic |
| `EVENT_CONFIGURATION_SET` | `seskit` | The SES configuration set sends go through |
| `PUBLIC_BASE_URL` | — | Where SNS can reach this instance, for `https` ingestion |
| `WEBHOOK_ALLOWED_CIDRS` | — | Internal ranges webhooks may reach in production |
| `WEBHOOK_MAX_ATTEMPTS` | `6` | Delivery attempts before a webhook is abandoned |
| `SMTP_HOST` | — | Local delivery target. Points at Mailpit in development |
| `API_RATE_LIMIT_PER_MINUTE` | `100` | Per project, not per key |
| `POSTGRES_HOST_PORT` | `55432` | Compose only. See [install](../getting-started/installation.md) |
| `REDIS_HOST_PORT` | `56379` | Compose only |

## There is no AWS credential setting

!!! note "Deliberately absent"
    Credentials are resolved by boto3 from the environment SESKit runs in and
    are never handled as data — naming them here would invite them into logs
    and into config dumps.

    In production the right answer is an IAM role, which has nothing to name.
    See [Connect an AWS account](../guides/connect-aws.md).

## The ones worth thinking about

**`SECRET_KEY`** signs session cookies. Changing it signs everyone out. It
refuses to boot on the example value, because an instance running with a
publicly known signing key is an instance anyone can forge a session for.

**`ALLOW_SIGNUP`** is `false`, and registration opens anyway while the instance
has no accounts — so the first person to arrive claims it and the door closes
behind them. An instance you deploy is yours, not an open sign-up page someone
else can find.

**`EVENT_INGESTION`** decides how delivery events get back. `sqs` works from
anywhere including a laptop; `https` is lower latency but needs SESKit
reachable from the internet with a certificate. See
[delivery events](../guides/delivery-events.md).

**`WEBHOOK_ALLOWED_CIDRS`** is the deliberate hole in the SSRF defence. Only
list ranges you actually need to reach, and understand that response bodies
from those endpoints are captured into your delivery log.
