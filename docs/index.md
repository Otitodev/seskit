# SESKit

A Python-native developer email platform built on Amazon SES. It makes SES feel
as simple as Resend, without giving up ownership of your sending
infrastructure — you run it on your own AWS account, and the mail, the
recipients and the delivery history stay there.

## Two halves, and only one of them is the product

SESKit ships two things under one name. Knowing which is which saves an hour.

```text
their application                  their server
┌─────────────────────┐           ┌────────────────────────┐
│ pip install seskit  │  ─HTTP→   │ git clone + compose up │ ──→ Amazon SES
│ client.emails.send()│           │ API, dashboard, worker │
└─────────────────────┘           └────────────────────────┘
        the client                        the server
```

**The repository is the server.** You clone it and run `docker compose up`
once, on whatever machine you run things on. That gives you the HTTP API, the
dashboard, the background worker, Postgres and Redis. This is SESKit.

**The package is a client.** `pip install seskit` goes inside *your own
application* — the one that wants to send mail — and makes HTTP calls to your
running server. Usually a different machine, often a different codebase.

If that pairing feels familiar, it should: you install and run PostgreSQL, and
separately your application installs a driver to talk to it. Nobody thinks of
those as one installation done twice.

**The client is optional.** It is a thin wrapper over the same HTTP API `curl`
reaches, so anything you can do with it you can do with an HTTP request.
Business logic lives in the API and is never duplicated in a client, which is
why a Python call and a `curl` command cannot drift apart.

!!! warning "The SDK is not written yet"
    The `seskit` package on PyPI currently reserves the name and contains no
    working client. Talk to the HTTP API directly for now — that is the
    supported path, and the one every guide here uses.

## Start here

<div class="grid cards" markdown>

- **[Install and run](getting-started/installation.md)**

    Get the server up with Docker Compose.

- **[Your first email](getting-started/first-email.md)**

    About a minute, through Mailpit, with no AWS account at all.

- **[No AWS account yet?](guides/no-aws-account-yet.md)**

    The five-minute path to a free-tier account and the SES sandbox.

- **[Reading your metrics](guides/metrics.md)**

    What the six counts mean, and what each rate divides by.

</div>

## You do not need AWS to try it

A brand-new AWS account cannot send to arbitrary recipients for roughly 24
hours — leaving the SES sandbox is a support review, and no amount of interface
design removes that wait. So first success does not depend on it.

Out of the box, SESKit sends through a local Mailpit inbox. `POST /v1/emails`
works immediately after `docker compose up`, and the message appears at
`http://localhost:8025`. No AWS account, no sandbox, no verified domain.

## Why it exists

Amazon SES is the cheapest reliable way to send transactional email, and one of
the least pleasant to adopt. Identities, DKIM records, configuration sets,
event destinations, SNS topics and sandbox limits all have to be understood
before the first message arrives.

SESKit does that setup and gives you an API worth using, a dashboard that says
what happened, and delivery events that reconcile back to the message that
caused them. AWS handles the infrastructure; SESKit handles the developer
experience.

The comparable products are hosted, or written in TypeScript end to end, or
both. SESKit is self-hosted and Python throughout — a FastAPI backend for teams
who would rather not run a Node toolchain to send email.
