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
| **Inbox** | Mail received on your domains, newest first |
| **Domains** | Sending identities and their verification state, with the DNS records to add, and the switch for receiving |
| **AWS** | The connection, the region, your quota, sandbox status, and event reporting setup |
| **API keys** | Create and revoke. Shown once at creation |
| **Webhooks** | Endpoints, signing secrets, and the delivery history for each |
| **Suppressions** | Addresses this project will not send to, why, and the button that puts one back |
| **Settings** | Your account's recovery code |

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

A verified domain also has a **Start receiving** button. If it is greyed out, the
card says why. See [receive email](../guides/receive-email.md).

## Inbox

Mail sent to your domains, newest first. With more than one domain, the buttons
above the list narrow it to one; the view is a link you can share.

A message's page shows the sender, recipients and subject, the text body, the
HTML body, the attachments, the headers, and the SPF, DKIM, DMARC, spam and virus
verdicts Amazon SES recorded. A verdict reads "not checked" when SES did not
check it, which is not the same as passing.

Everything in a received message came from someone else, so the HTML body is
shown in a sandbox: no script runs and nothing loads from the network. Attachments
download as opaque bytes whatever type they claim. **Download .eml** gives you the
original message, until its retention period passes.

Until a domain is receiving, the page says so and links to Domains. See
[receive email](../guides/receive-email.md).

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

## Suppressions

Every address the project is refusing to send to, why it is there, and — where
a message caused it — a link to that message.

The page exists mostly for **Allow again**. Suppression happens on its own; the
question it generates is "why can I not email this person?", and that arrives
as a support ticket rather than as a stack trace. An empty list is the healthy
state, not a setup step.

An address can also be added by hand, for the case automation cannot cover:
somebody asks to be left alone and there is no bounce to record it. Full
detail in the [suppression guide](../guides/suppression.md).

## Settings

Whether your account has a recovery code, and a way to make a new one. A new
code replaces the old one, and you are asked for your current password first. The
code is shown once and stored only as a hash. See
[I have forgotten my dashboard password](../operating/troubleshooting.md#i-have-forgotten-my-dashboard-password).

## Dark mode

Three states: light, dark, and following your system. The choice is remembered
in your browser and applied before first paint, so a dark-theme user never sees
a light flash on load.
