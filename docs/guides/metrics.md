# Reading your metrics

The Overview turns [delivery events](delivery-events.md) into the numbers you
actually get judged on. Everything here is computed live from PostgreSQL —
nothing is precomputed and nothing is cached, because a stale delivery metric
is worse than a slow one.

## Counts

| | |
|---|---|
| **Sent** | messages SESKit handed to a provider, and the provider accepted |
| **Delivered** · **Bounced** · **Complained** · **Opened** · **Clicked** | messages with at least one event of that kind |

The five event counts are **distinct messages, never event rows**. Amazon SES
emits an `Open` every time a message is opened, so counting rows would produce
open rates above 100% — which reads as a bug and costs you trust in every other
number on the page.

## Rates, and what each one divides by

```text
Delivery rate    delivered  / sent
Bounce rate      bounced    / sent        ← the number AWS suspends accounts over
Complaint rate   complained / sent        ← and this one
Open rate        opened     / delivered
Click rate       clicked    / delivered
```

**Bounce and complaint divide by *sent* because that is what AWS divides by.**

Computing them against *delivered* would be flattering, smaller than the figure
in your SES console, and wrong in the one direction that gets an account
suspended while your own dashboard still looks healthy. SESKit shows a warning
above AWS's published review thresholds — **5% bounce, 0.1% complaint**.

**Open and click divide by *delivered***, because a message that never arrived
could not be opened. Dividing by sent would depress both rates by exactly your
bounce rate.

## An empty denominator is a dash

Every rate is shown as `—` when its denominator is zero. `0%` would assert
something untrue about an empty account: not that nothing was delivered out of
things that were sent, but that nothing was sent at all.

## "Not tracked" is not 0%

Open and click tracking is
[off unless you turn it on](delivery-events.md#open-and-click-tracking). With
it off, both rates read **Not tracked** rather than `0%` — the truth is not
that nobody opened your mail, it is that nobody was counting.

The distinction matters because `0%` is a number someone might act on.

## Time ranges

**24 hours**, **7 days** and **30 days**, as a query parameter (`/?range=7d`),
so a view is linkable and survives a refresh.

Events are filtered on **when they happened**, not when SESKit heard about
them: a queue backlog that delivers a bounce an hour late still files it under
the hour it occurred. Filtering the other way would make a backlog look like a
spike.

## The chart is an enhancement

The numbers are server-rendered HTML. The activity chart on top of them is an
enhancement — with JavaScript blocked or broken you lose the picture, not the
information.

Chart.js is vendored rather than loaded from a CDN; its version, hash and
provenance are recorded beside it in the repository. A self-hosted dashboard
that breaks when a CDN is unreachable, or phones out on every page load, would
contradict the point of self-hosting.

## What to actually watch

If you read one number, read the **bounce rate**. It is the one that ends
accounts, and it climbs quietly: a list with stale addresses looks fine at 2%
and gets reviewed at 5% without anything visibly changing.

Complaint rate has less headroom than it looks — 0.1% is one complaint in a
thousand messages.
