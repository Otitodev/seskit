# SESKit — MVP Product Specification

## 1. Product Overview

**SESKit** is a Python-native, open-source developer email platform that makes Amazon Simple Email Service (Amazon SES) feel as simple to use as Resend.

The product sits between an application and Amazon SES:

```text
Application
    |
    | SESKit API / SDK
    v
SESKit
    |
    | AWS SDK
    v
Amazon SES
    |
    v
Recipients
```

The MVP should focus on transactional email and developer experience rather than attempting to reproduce every Resend feature.

### Core value proposition

> Connect an AWS account, configure SES, get an API key, and start sending production email through a clean Resend-like API and dashboard.

### Primary target users

- Python/FastAPI developers
- SaaS developers
- Indie hackers
- Startups
- Teams already using AWS
- Developers who want Resend-like DX without paying a separate email-platform markup
- Developers who want to self-host their email platform

### Positioning

SESKit is the **Python-native alternative** in this space. Open-source Resend-on-SES wrappers already exist (useSend/Unsend, Plunk), but both are JS/TS end-to-end (Next.js/Prisma dashboards, TypeScript SDKs). SESKit's wedge is a FastAPI backend and a first-class Python SDK for teams who don't want a Node toolchain in their stack to self-host their email infrastructure. This is why the Python SDK (§13) is core MVP surface, not a late-phase add-on.

---

# 2. MVP Goals

The MVP must allow a user to:

1. Create an SESKit account.
2. Connect an AWS account securely.
3. Select an AWS region.
4. Configure/verify a sending domain.
5. Generate an SESKit API key.
6. Send email through a simple REST API.
7. Send email through a Python SDK.
8. View email logs.
9. Track SES delivery events.
10. Receive webhook events.
11. View basic email statistics.
12. Manage API keys.
13. Disconnect an AWS account.
14. Run the entire system locally with Docker.

---

# 3. Non-Goals for MVP

Do **not** implement these in the first version:

- Marketing campaigns
- Contacts
- Segmentation
- Visual drag-and-drop email editor
- Email automation workflows
- Inbound email
- Dedicated IP management
- Multi-provider support
- Billing/subscriptions
- Team collaboration
- Advanced RBAC
- AI email generation
- Complex email builder
- Full Resend feature parity
- Mobile application
- Multi-region sending per project (an AWSConnection is single-region for MVP; a project needing another region creates a second connection)
- Hosted, delegated AWS access via cross-account AssumeRole (see §9 — MVP targets self-hosted deployments using standard boto3 credential resolution; a hosted multi-tenant connection flow is a post-MVP item, see §34)

The architecture should allow these to be added later without rewriting the core system.

---

# 4. Product Architecture

```text
                         ┌───────────────────────┐
                         │      Web Dashboard    │
                         │       Next.js         │
                         └───────────┬───────────┘
                                     │
                                     v
                         ┌───────────────────────┐
                         │      FastAPI API      │
                         │                       │
                         │ Auth                  │
                         │ API Keys              │
                         │ Emails                │
                         │ Domains               │
                         │ Webhooks              │
                         │ Analytics             │
                         └───────────┬───────────┘
                                     │
                 ┌───────────────────┼──────────────────┐
                 │                   │                  │
                 v                   v                  v
          ┌────────────┐      ┌────────────┐     ┌────────────┐
          │ PostgreSQL │      │   Redis    │     │  Worker    │
          │            │      │            │     │            │
          │ Users      │      │ Queues     │     │ SES events │
          │ Projects   │      │ Rate limit │     │ Webhooks   │
          │ Emails     │      │ Cache      │     │ Jobs       │
          │ Domains    │      └────────────┘     └─────┬──────┘
          └────────────┘                               │
                                                       v
                                              ┌────────────────┐
                                              │   AWS SES      │
                                              │                │
                                              │ Send           │
                                              │ Domain config  │
                                              │ Events         │
                                              └────────────────┘
```

---

# 5. Recommended Technology Stack

## Backend

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic
- PostgreSQL
- boto3
- Redis
- Background worker: ARQ or Celery
- Pytest
- Ruff
- mypy where practical

## Frontend

- Jinja2 (server-rendered by the FastAPI app)
- HTMX
- Hand-written CSS with custom properties
- Alpine.js, only where a component genuinely needs client state
- Small vendored JS libraries (e.g. Chart.js) where charts are required

**There is no Node.js in this project.** No npm, no `node_modules`, no
JavaScript build step, no separate frontend service. The dashboard is served by
the same Python process as the API.

This is a positioning decision, not just a stack preference (§1): a self-hoster
runs one Python service. A build pipeline that required a Node toolchain — or a
100MB+ CSS compiler binary — would contradict the product's own pitch.

Server-rendered does not mean unpolished. The dashboard should look like a
developer infrastructure product, not an admin template. Design direction,
tokens, and the component vocabulary live in `docs/design-system.md`; read it
before building any dashboard page.

## Infrastructure

- Docker
- Docker Compose for local development
- Amazon SES
- AWS IAM
- Amazon SNS/EventBridge where appropriate for SES events
- S3 only if required by a later feature

---

# 6. Core Domain Model

The application should use a multi-tenant architecture.

## User

```text
id
email
password_hash / auth_provider
created_at
updated_at
```

## Project

A user may have one or more projects.

```text
id
user_id
name
created_at
updated_at
```

## AWSConnection

Stores a logical connection to an AWS account.

```text
id
project_id
aws_account_id
region
credential_mode
encrypted_credentials / role configuration
status
created_at
updated_at
```

Never store plaintext AWS secret keys.

Prefer short-lived credentials or IAM role assumption for hosted deployments.

## Domain

```text
id
project_id
domain
verification_status
dkim_status
mail_from_status
created_at
updated_at
```

## APIKey

```text
id
project_id
name
key_prefix
hashed_key
last_used_at
created_at
revoked_at
```

The raw API key must only be shown once.

## Email

```text
id
project_id
provider_message_id
from_address
to_addresses
cc_addresses
bcc_addresses
reply_to
subject
status
html_body
text_body
scheduled_at
sent_at
delivered_at
created_at
updated_at
```

Do not expose email bodies unnecessarily in logs.

Consider configurable retention for sensitive content.

## EmailEvent

```text
id
email_id
event_type
provider_event_id
payload
occurred_at
created_at
```

Supported MVP event types:

```text
sent
delivered
bounced
complained
opened
clicked
```

## WebhookEndpoint

```text
id
project_id
url
secret
enabled
created_at
updated_at
```

## WebhookDelivery

```text
id
webhook_endpoint_id
event_id
status
attempt_count
response_status
last_attempt_at
next_attempt_at
created_at
```

---

# 7. Authentication

The dashboard needs normal user authentication.

For MVP:

- Email/password authentication is acceptable.
- OAuth can be added later.
- Session-based authentication or secure JWT architecture is acceptable.

The API used by customer applications must use SESKit API keys.

Example:

```http
Authorization: Bearer sk_live_xxxxxxxxx
```

API keys must be:

- hashed at rest
- revocable
- scoped to a project
- shown only once
- prefixed for easy identification

---

# 8. AWS Integration

AWS integration is the most important part of the product.

## MVP connection flow

```text
User
 |
 | Connect AWS
 v
AWS connection wizard
 |
 | Select region
 v
Validate AWS access
 |
 v
Get AWS account ID
 |
 v
Check SES status
 |
 v
Check sandbox status
 |
 v
Display SES sending quota
 |
 v
Ready
```

The system should verify that the configured AWS identity has the required SES permissions.

**Sandbox check is required, not optional.** Every new AWS account/region starts in the SES sandbox (200 messages/24h, 1 msg/sec, verified recipients only). If this isn't surfaced during connect, a new user's first send fails for a reason the UI never explained. Detect sandbox status here and show it persistently on the AWS dashboard page (§17) with a link to AWS's production-access request flow until the account graduates.

---

# 9. AWS Credential Security

MVP targets **self-hosted deployments only**. SESKit runs with credentials for a single AWS account at a time, resolved the standard boto3 way:

- IAM role (EC2/ECS/EKS instance or task role)
- environment variables
- AWS credential file
- ECS/EKS workload identity where applicable

Do not build the MVP around storing permanent AWS access keys in the database — if a user supplies keys directly (e.g. local Docker Compose dev), treat them as configuration/secrets (env vars, secret manager), not as data persisted in the `AWSConnection` table. The `AWSConnection` model still exists (§6) but for MVP it records *which* credential source is active and its resolved account ID/region/status — it does not store or broker credentials for other AWS accounts.

**Deferred to post-MVP (§34):** a hosted, multi-tenant SESKit Cloud that manages *other* AWS accounts' SES on their behalf needs cross-account `AssumeRole` delegation:

```text
Customer AWS Account
        |
        | AssumeRole (with a SESKit-generated sts:ExternalId)
        v
SESKit AWS role
        |
        v
Amazon SES
```

When that's built: the external ID must be generated by SESKit per connection, never accepted from the customer (a common vendor bug — validate this specifically, it's a known confused-deputy vector), and the customer-side role should ship as a one-click CloudFormation/Terraform template rather than manual IAM console steps.

Document the minimum required IAM permissions for both paths.

Never request `AdministratorAccess`.

---

# 10. Domain Setup

The dashboard should provide:

```text
Domains
----------------------------
example.com

Status:
✓ Domain verified
✓ DKIM verified
✓ Sending enabled
```

The setup wizard should:

1. Accept a domain.
2. Call SES to create/configure the identity.
3. Retrieve verification/DKIM information.
4. Display DNS records.
5. Allow the user to copy records.
6. Poll SES for verification status.
7. Update the UI automatically.

The MVP may require users to manually add DNS records.

Automatic DNS provider integrations are out of scope.

**Open/click tracking domain:** SES tracks opens/clicks (§6, §16) via a configuration-set `TrackingOptions` redirect. For MVP, use SES's default tracking domain (no extra DNS record required) rather than a custom tracking subdomain — a custom tracking CNAME + HTTPS cert setup is real added scope to the wizard and is deferred to V1.1 (§34). This is a deliberate MVP choice, not an oversight: document it in the dashboard so users know links/pixels are served from an Amazon-operated domain, not their own.

---

# 11. Email Sending API

Primary endpoint:

```http
POST /v1/emails
```

Example:

```json
{
  "from": "Acme <hello@example.com>",
  "to": ["user@example.com"],
  "subject": "Welcome to Acme",
  "html": "<h1>Welcome!</h1>",
  "text": "Welcome to Acme"
}
```

Response:

```json
{
  "id": "email_01J...",
  "status": "queued"
}
```

The API should support:

- from
- to
- cc
- bcc
- reply_to
- subject
- html
- text
- attachments
- custom headers where SES permits them
- idempotency key

**Attachment size limit:** enforce a request-level cap aligned to SES's raw message ceiling (10MB total for `SendRawEmail`) and reject oversized requests at the API boundary with `attachment_too_large` (§19) rather than passing through a raw SES `MessageRejected` error.

---

# 12. Idempotency

Support:

```http
Idempotency-Key: <unique-key>
```

If the same idempotency key is submitted again, the API must return the existing email record instead of sending a duplicate email.

Store idempotency keys per project.

---

# 13. Python SDK

Provide a simple Python SDK.

Example:

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_xxx")

result = client.emails.send(
    from_="hello@example.com",
    to=["user@example.com"],
    subject="Welcome",
    html="<h1>Hello!</h1>",
)
```

The SDK should be a thin client over the HTTP API.

Do not duplicate business logic inside the SDK.

The SDK should support:

```text
emails.send()
emails.get()
emails.list()
```

Additional SDK functionality can be added later.

---

# 14. Email Processing Pipeline

Email sending should not block unnecessarily on slow provider operations.

Recommended flow:

```text
API request
    |
    v
Validate request
    |
    v
Create Email record
    |
    v
Queue send job
    |
    v
Worker
    |
    v
SES SendEmail / SendRawEmail
    |
    v
Store provider message ID
    |
    v
Return / update status
```

For MVP, synchronous SES sending may be acceptable if implementation simplicity is important.

However, design the service so it can move to queue-based sending without changing the public API.

---

# 15. SES Event Processing

SES events should be converted into internal events.

Example:

```text
SES
 |
 v
SNS / EventBridge
 |
 v
Webhook/Event Receiver
 |
 v
Normalize Event
 |
 v
Find Email by provider_message_id
 |
 v
Create EmailEvent
 |
 v
Update Email status
 |
 v
Queue customer webhook
```

Provider-specific event payloads should not leak directly into the public API.

Create an internal normalized event schema.

Example:

```json
{
  "id": "evt_01J...",
  "type": "email.delivered",
  "email_id": "email_01J...",
  "created_at": "2026-08-24T20:00:00Z",
  "data": {
    "to": "user@example.com"
  }
}
```

---

# 16. Webhooks

MVP webhook events:

```text
email.sent
email.delivered
email.bounced
email.complained
email.opened
email.clicked
```

Webhook requests should include a signature.

Example concept:

```http
X-SESKit-Signature: ...
```

Use HMAC signing.

Webhook delivery requirements:

- retry failed deliveries
- exponential backoff
- record response status
- record attempt count
- allow endpoint disablement
- provide delivery history in dashboard

MVP retry policy can be simple, for example:

```text
Attempt 1
Attempt 2
Attempt 3
Attempt 4
Attempt 5
```

with increasing delays.

---

# 17. Dashboard

Server-rendered from the FastAPI app with Jinja2 and HTMX (§5). Pages are
composed from the shared component macros; design direction and the component
vocabulary are in `docs/design-system.md`.

The dashboard should contain these pages.

## Overview

Display:

```text
Emails sent
Delivered
Bounced
Complaints
Open rate
Click rate
```

Use simple time-range filters:

```text
24 hours
7 days
30 days
```

## Emails

Table:

```text
ID
Recipient
Subject
Status
Created
```

Clicking an email shows:

```text
Message ID
From
To
Subject
Status
Timeline
Events
```

## Domains

Display configured domains and verification status.

## API Keys

Allow:

- create
- revoke
- view key prefix
- see last used time

## Webhooks

Allow:

- create endpoint
- edit endpoint
- enable/disable
- delete
- view delivery attempts

## AWS

Display:

- connected account
- region
- SES status
- sending quota
- connection status

---

# 18. Basic Analytics

MVP analytics should be derived from normalized EmailEvent records.

Metrics:

```text
Sent
Delivered
Bounced
Complained
Opened
Clicked
```

Calculate:

```text
Delivery rate
Bounce rate
Complaint rate
Open rate
Click rate
```

Do not over-engineer analytics in V1.

PostgreSQL aggregation is sufficient for the initial version.

---

# 19. Error Handling

The API must normalize AWS errors.

Do not expose raw boto3/AWS exceptions to customers.

Example:

```json
{
  "error": {
    "type": "domain_not_verified",
    "message": "The sending domain has not been verified in Amazon SES."
  }
}
```

Potential error types:

```text
invalid_request
authentication_failed
authorization_failed
domain_not_verified
sending_limit_exceeded
rate_limit_exceeded
provider_error
invalid_recipient
attachment_too_large
email_rejected
internal_error
```

---

# 20. Rate Limiting

Implement project-level API rate limiting.

Redis is recommended.

Example initial limits:

```text
100 requests/minute/project
```

The limit should be configurable.

Do not treat this as an email-volume limit. SES itself has sending quotas that must also be respected.

---

# 21. Observability

MVP should include structured application logs.

Every email request should have a request ID.

Example:

```text
request_id
project_id
email_id
provider_message_id
event_type
timestamp
```

Use structured JSON logging.

Do not log:

- AWS secret keys
- API keys
- full email bodies by default
- sensitive credentials

---

# 22. Security Requirements

Security is a first-class requirement.

Must implement:

- hashed API keys
- encrypted sensitive credentials
- HTTPS in production
- secure cookies
- CSRF protection where applicable
- input validation
- SQL injection protection through SQLAlchemy
- rate limiting
- webhook signature verification
- least-privilege AWS permissions
- secret redaction in logs

Never store raw API keys.

Never expose AWS credentials through the frontend.

---

# 23. API Documentation

FastAPI should generate OpenAPI documentation.

Provide:

```text
/v1/emails
/v1/emails/{id}
/v1/domains
/v1/api-keys
/v1/webhooks
```

The API should be documented with examples.

The OpenAPI schema should be usable for generating client SDKs later.

---

# 24. CLI

CLI is optional for the first implementation but the architecture should allow it.

Potential future commands:

```bash
seskit login
seskit aws connect
seskit domains list
seskit emails send
seskit logs
```

Do not prioritize CLI over the core API/dashboard.

---

# 25. Local Development

The entire stack should run locally with:

```bash
docker compose up
```

Services:

```text
api        (also serves the dashboard - there is no separate frontend service)
worker
db         (PostgreSQL)
redis
mailpit
```

Postgres and Redis are published on non-standard host ports (55432 / 56379) by
default. A developer machine with PostgreSQL installed often already has
clusters bound to 5432 *and* 5433; those win the binding, and the container
then reports healthy while every connection from the host silently reaches the
wrong database.

Provide:

```text
.env.example
docker-compose.yml
README.md
```

**Local email provider: Mailpit.** Follow the pattern used by the FastAPI full-stack template (`fastapi/full-stack-fastapi-template`): add a `mailpit` service (`axllent/mailpit`, MIT-licensed, single container — SMTP on 1025, web inbox UI on 8025) to `docker-compose.yml`, and point the backend's `SMTPProvider` (§26) at it by default:

```text
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_TLS=false
```

A project with no completed AWS connection sends through `SMTPProvider` → Mailpit by default. `POST /v1/emails` works immediately after `docker compose up`, with the email visible at `http://localhost:8025` — no AWS account, no SES sandbox, no verified domain required to try the product or run the test suite. Mailpit's REST API (not just the web UI) lets integration tests assert on captured subject/body/headers programmatically, which is useful in Phase 6 alongside the moto/mailbox-simulator work already planned for Phase 4 — Mailpit exercises the API→queue→worker→provider pipeline end-to-end; moto/mailbox-simulator exercises SES-specific behavior (identity verification, quotas, bounces) that Mailpit can't simulate.

**Switching to SES.** Once a project's AWS connection (§8) is established and a domain is verified (§10), sending for that project transparently switches to `SESProvider`. This is a provider-selection state change per project, not a code change for the calling application — same `/v1/emails` request, same SDK call, before and after.

Do not require developers to have production SES credentials merely to run the application.

---

# 26. Provider Abstraction

Although MVP supports only Amazon SES, create a provider interface.

Example:

```python
class EmailProvider(Protocol):

    async def send_email(...):
        ...

    async def get_domain_status(...):
        ...

    async def get_sending_quota(...):
        ...
```

Implement:

```text
EmailProvider
    |
    ├── SESProvider
    └── SMTPProvider   (local/test only — see §25)
```

This keeps the door open for:

```text
SES
Postmark
Mailgun
SendGrid
SMTP
```

in a future release, as **production** alternatives to SES.

Do not implement Postmark/Mailgun/SendGrid in MVP. `SMTPProvider` is the one exception, built now — not as an alternative production provider (that stays a non-goal, §3), but purely as the local-development backend described in §25. It sends via plain SMTP to whatever `SMTP_HOST` points at, the same interface a production SMTP provider would eventually implement, so pulling it forward costs nothing architecturally.

---

# 27. Resend Compatibility

The product should be conceptually compatible with the Resend API, but do not copy proprietary implementation details.

Where practical, use familiar concepts:

```text
emails.send
API keys
domains
webhooks
email IDs
email events
templates
```

The MVP does not need 100% API compatibility.

A future compatibility layer can expose:

```text
/v1/emails
```

with request/response structures close to Resend.

The long-term goal is:

```text
Existing Resend application
        |
        | minimal configuration change
        v
SESKit
        |
        v
Customer AWS SES
```

---

# 28. Open Source Strategy

The core project should be designed as an open-source application.

Recommended repository structure:

```text
seskit/
├── apps/
│   ├── api/          # FastAPI + Jinja2 templates + static assets
│   └── worker/       # ARQ background worker
├── packages/
│   ├── sdk-python/
│   ├── core/
│   └── provider-aws-ses/
├── migrations/
├── tests/
├── docs/
├── docker/
├── docker-compose.yml
├── pyproject.toml    # uv workspace root
├── .env.example
├── LICENSE
└── README.md
```

There is no `apps/web`: the dashboard is server-rendered templates inside
`apps/api` (§5). `apps/api` and `apps/worker` both depend on `packages/core`;
neither depends on the other, and provider-specific code stays isolated in its
own package (§32.8, §32.12).

Keep proprietary hosted-service functionality separate from the core open-source engine if a commercial hosted version is introduced later.

## License

**MIT.** Matches Python-ecosystem norms (boto3, FastAPI, SQLAlchemy are all permissively licensed) and the self-hosted-first positioning (§1). This is compatible with the open-core split above: a future hosted-service layer simply lives in a separate, unlicensed repository — MIT on the core doesn't obligate that layer to be open. Add the `LICENSE` file in Phase 1, before any external contributions land; relicensing later gets legally messy once outside contributors are involved.

---

# 29. Testing Requirements

The coding agent must create tests while implementing the system.

Minimum test coverage areas:

## Unit tests

- API key hashing
- API key validation
- email validation
- idempotency
- domain state handling
- SES provider mapping
- event normalization
- webhook signatures
- retry calculation

## Integration tests

- create project
- create API key
- send email
- retrieve email
- list emails
- create domain
- process SES event
- create webhook
- deliver webhook

## API tests

Test all public endpoints.

## Frontend tests

At minimum test:

- login
- dashboard rendering
- email list
- domain setup
- API key creation
- webhook creation

---

# 30. MVP Acceptance Criteria

The MVP is considered complete when a new developer can:

### Setup

```text
1. Clone repository
2. Run docker compose up
3. Open dashboard
4. Create account
5. Connect AWS
6. Configure SES region
7. Add sending domain
8. Verify DNS
9. Generate API key
```

### Send

Then run:

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_xxx")

email = client.emails.send(
    from_="hello@example.com",
    to=["test@example.com"],
    subject="Hello",
    html="<h1>Hello from SESKit</h1>",
)
```

The email must:

```text
Application
    ↓
SESKit
    ↓
Amazon SES
    ↓
Recipient
```

### Observe

The developer can then open the dashboard and see:

```text
Email
Status: Delivered
Provider ID: ...
Created: ...
Delivered: ...
```

and see corresponding events.

### Webhook

The developer can configure:

```text
https://example.com/webhooks/email
```

and receive:

```json
{
  "type": "email.delivered",
  "email_id": "email_..."
}
```

with a valid signature.

---

# 31. Build Order

The coding agent should implement in this order.

## Phase 1 — Project foundation ✅ complete

- Reserve the "seskit" namespace on PyPI and GitHub before any public commits — **still outstanding, user action**
- Add `LICENSE` (MIT, §28)
- Repository structure (uv workspace: `apps/{api,worker}`, `packages/{core,provider-aws-ses,sdk-python}`)
- Docker Compose: api, worker, db, redis, mailpit (§25)
- FastAPI application factory
- PostgreSQL + async SQLAlchemy 2 (asyncpg)
- Redis (async client, shared by API and worker)
- Alembic (async template, wired to settings and `Base.metadata`)
- configuration management (Pydantic Settings; refuses placeholder secrets outside local)
- logging (structlog JSON, request-ID middleware, secret redaction — §21/§22)
- health checks (`/healthz` liveness, `/readyz` readiness)
- ARQ worker with a `ping` job proving the queue round-trips
- UI foundation: design tokens, app shell, component macros, empty states (§5, `docs/design-system.md`)
- Tests, ruff, mypy (strict), pre-commit, GitHub Actions CI

## Phase 2 — Authentication and projects

- User model
- authentication
- project model
- project selection
- authorization

## Phase 3 — API keys

- generation
- hashing
- validation
- revocation
- middleware/auth dependency

## Phase 4 — AWS SES provider

- boto3 integration
- AWS connection (standard credential resolution, self-hosted — §9)
- account verification
- sandbox status detection (§8)
- region handling
- quota retrieval
- error normalization
- spike: confirm `moto`'s SESv2 mock coverage before writing tests against it; where it's thin, fall back to SES mailbox simulator addresses against a real sandboxed test account for integration tests

## Phase 5 — Domain management

- create domain
- SES identity configuration
- verification status
- DKIM records
- DNS instructions

## Phase 6 — Email API

- send endpoint
- email persistence
- `SMTPProvider` (Mailpit, §25/§26) — build this first, it unblocks testing every later phase without AWS
- SES send
- per-project provider selection (SMTP until AWS connected + domain verified, then SES — §8)
- provider message IDs
- attachments
- idempotency

## Phase 7 — Event processing

- SES event ingestion
- event normalization
- EmailEvent model
- email status updates

## Phase 8 — Webhooks

- endpoint management
- HMAC signatures
- delivery queue
- retry mechanism
- delivery logs

## Phase 9 — Dashboard

Built from the component macros and design tokens established in Phase 1 — do
not restyle per page, and read `docs/design-system.md` first.

- overview (replace Phase 1's placeholder with real metrics)
- emails
- email details
- domains
- AWS connection
- API keys
- webhooks
- basic analytics
- vendor Alpine.js and Chart.js at this point, when there is finally something that needs them

## Phase 10 — Python SDK

- package
- authentication
- emails.send
- emails.get
- emails.list
- documentation
- tests

## Phase 11 — Hardening

- security review
- rate limits
- validation
- error handling
- integration tests
- documentation
- local setup verification

---

# 32. Coding Agent Rules

The coding agent should:

1. Work incrementally.
2. Keep commits/tasks small and verifiable.
3. Write tests alongside implementation.
4. Run tests after each major subsystem.
5. Never skip migrations.
6. Never hard-code credentials.
7. Never commit secrets.
8. Keep provider-specific logic isolated.
9. Avoid premature abstractions.
10. Avoid implementing non-MVP features.
11. Prefer boring, maintainable architecture over unnecessary complexity.
12. Preserve a clean separation between API, domain logic, provider integrations, persistence, and background jobs.

---

# 33. Definition of Done

A feature is not complete merely because the code compiles.

For each feature:

```text
Implementation
    ↓
Unit tests
    ↓
Integration/API tests
    ↓
Manual verification
    ↓
Documentation
    ↓
Error handling
    ↓
Security review
```

The MVP should be deployable by a technically competent user using Docker and an AWS account.

---

# 34. Future Roadmap

After MVP:

### V1.1

- React Email integration
- Template API
- Template versioning
- Scheduled emails
- Batch sending
- Better analytics
- Email search/filtering
- Custom open/click tracking domain (deferred from MVP, §10)
- AI-assisted DNS/domain diagnostics (see "Agentic developer tooling" below)

### V1.2

- Contacts
- Audiences
- Broadcasts
- Suppression management
- Advanced webhook management
- CLI

### V2

- Inbound email
- Visual template editor
- Email automation
- Multi-provider support
- Team accounts
- RBAC
- Billing
- Hosted SESKit Cloud — this is where cross-account `AssumeRole` delegation (deferred from MVP §9) gets built, since it's only needed once SESKit manages *other people's* AWS accounts

### Agentic developer tooling

Distinct from the MVP non-goal "AI email generation" (§3, which refers to AI-authored marketing content/campaigns — still out of scope). These are developer-support tools, in the same spirit as §35's "AWS handles infrastructure, SESKit handles developer experience":

- **MCP server for SESKit.** Expose domains/API-keys/send/webhooks as MCP tools alongside the Python SDK (§13), so AI coding agents (Claude Code, Cursor, etc.) building an app against SESKit can create a test domain, generate an API key, and send a test email as part of the agent's own workflow — the same DX shortcut SESKit gives human developers, extended to the agents increasingly writing the integration code for them. Natural fit given the target audience (Python/FastAPI developers, indie hackers) already leans on coding agents.
- **DNS/deliverability diagnostic assistant.** DKIM/SPF/DMARC misconfiguration is the single biggest support burden for any ESP. An assistant that reads a domain's current verification/DKIM/mail-from status (already modeled in §6's `Domain` entity) and the actual DNS records a user has published, then explains in plain language what's missing or wrong, turns a support ticket into a self-serve fix. Grounded entirely in data SESKit already collects — no new infrastructure beyond an LLM call.
- **"Why did this bounce" event assistant.** A natural-language query surface over `EmailEvent` (§6) and SES bounce/complaint payloads — e.g. "why is delivery to this domain failing" — summarizing patterns (hard vs. soft bounces, a specific recipient domain rejecting mail, a spike in complaints) that are currently just rows in a table. Also grounded in existing data; no content-generation surface, so it doesn't reopen the "AI email generation" non-goal.

None of these belong in MVP — they're listed here so they're on the roadmap rather than rediscovered later, and so implementation of §6's data model (Domain, Email, EmailEvent) keeps them in mind (e.g. don't discard raw SES error payloads needed for the diagnostic assistant's explanations).

### Long-term

```text
SESKit
   |
   ├── Amazon SES
   ├── Postmark
   ├── Mailgun
   ├── SendGrid
   └── SMTP
```

The long-term product becomes a developer-focused email control plane rather than merely an SES wrapper.

---

# 35. Product Principle

The core principle of SESKit is:

> **AWS should handle email infrastructure. SESKit should handle developer experience.**

Do not attempt to compete with Amazon SES on infrastructure.

Do not attempt to build a second global email network.

Build the layer that turns:

```text
AWS account
+
SES
+
IAM
+
DNS
+
SNS/EventBridge
+
configuration sets
+
event processing
```

into:

```text
Connect AWS
       ↓
Verify domain
       ↓
Create API key
       ↓
Send email
       ↓
See what happened
```

That is the MVP.
