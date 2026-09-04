# The dashboard

Server-rendered by the same process that serves the API. There is no separate
frontend, and every page works with JavaScript disabled — HTMX upgrades the
filters and range controls into fragment swaps, and without it they are
ordinary links that still work.

## The pages

| | |
|---|---|
| **Overview** | The six counts and five rates, over 24 hours, 7 days or 30 days, with an activity chart |
| **Emails** | Everything this project sent, filterable by status. Each row links to the message |
| **Domains** | Sending identities and their verification state, with the DNS records to add |
| **AWS** | The connection, the region, your quota, sandbox status, and event reporting setup |
| **API keys** | Create and revoke. Shown once at creation |
| **Webhooks** | Endpoints, signing secrets, and the delivery history for each |

## Overview

The numbers you get judged on. Bounce and complaint rates divide by *sent* —
the denominator AWS uses — and a rate with no denominator shows `—` rather than
`0%`.

Worth reading [what each rate divides by](../guides/metrics.md) once, properly.
It is the page where a metric is easiest to get quietly wrong, and the
denominators are not the obvious ones.

The range is a query parameter (`/?range=7d`), so a view you are looking at is
a view you can link to.

## Emails

Every message with its status: `queued`, `sending`, `sent` or `failed`. The
filter narrows the list without moving the totals above it — those are the
project's totals, and a "Total" that changed when you clicked "Failed" would no
longer mean anything.

A message's own page shows what was sent, its provider message id, its
attachments by name, and a timeline of
[delivery events](../guides/delivery-events.md), most recent first.

!!! note "The HTML body is shown as source"
    Never rendered. The body is chosen by whoever holds an API key, and
    rendering it inside the account owner's session would execute their markup
    with a session cookie attached.

Blind copies are recorded but never listed beside the other recipients.

## Domains

Identities and their state. Unverified ones are re-checked every few hours,
verified ones monthly — the slow one catches a DKIM record removed long after
setup, which otherwise looks healthy right up until a send fails.

## AWS

The connection, and the one page that creates things in your account. Setting
up event reporting names the queue, topic and configuration set it will create
**before** you press the button, and disconnecting removes them again.

Sandbox status and quota are read when you connect and when you press
**Refresh**, not on every page load. The page says when it last looked.

## Webhooks

Each endpoint with its signing secret, its status, and its recent delivery
attempts — status code, response, and what went wrong. An endpoint SESKit
disabled after repeated failures says so and why, rather than showing a switch
that appears to have moved by itself.

## Dark mode

Three states: light, dark, and following your system. The choice is remembered
in your browser and applied before first paint, so a dark-theme user never sees
a light flash on load.
