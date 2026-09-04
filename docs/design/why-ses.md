# Why SES-native

## The gap this fills

Open-source Resend-on-SES platforms already exist, and they are good — but they
are JavaScript end to end. SESKit is for teams who would rather not add a Node
toolchain to their stack to self-host their email infrastructure:

- **A FastAPI backend and a first-class Python SDK**, not a TypeScript-only
  client.
- **One service to run.** The dashboard is server-rendered by the same Python
  process that serves the API — no separate frontend service, no
  `node_modules`, no JavaScript build step.
- **Your AWS account, your costs.** SESKit is the control plane; Amazon SES
  does the delivery.

## Why SES rather than an abstraction

SESKit could have defined a provider interface and supported SES, Postmark,
Mailgun and SendGrid behind it from the start. It deliberately does not.

An abstraction over several providers can only expose what they have in common,
and the interesting parts of email are exactly where they differ: SES's
configuration sets and event destinations, its sandbox, its account-level
reputation thresholds, its identity model. A lowest-common-denominator
interface would hide the specific things SESKit exists to make usable.

So the product is **SES made pleasant**, not *email made portable*. There is a
provider seam internally — `provider-aws-ses` and `provider-smtp` are separate
packages, and `core` defines the interface without importing either — but it
exists to make local development possible without AWS, not to promise
portability nobody asked for.

Multi-provider support is on the long-term roadmap, at the point where the
product is a control plane rather than an SES wrapper. That is a different
product, reached deliberately, not a constraint to design around now.

## The principle

> AWS should handle email infrastructure. SESKit should handle developer
> experience.

Amazon runs the sending infrastructure better and cheaper than anyone
self-hosting could. What it does not do is make it pleasant: identities, DKIM
records, configuration sets, event destinations, SNS topics and sandbox limits
all have to be understood before the first message arrives.

That gap is the entire product. SESKit does not try to be a better SES — it
tries to be the thing that means you never have to read the SES documentation.

Which is why the translation matters as much as the plumbing. A user should see
*"Domain verified · DKIM configured · Sending enabled"*, not *SES Identity*,
*MAIL FROM* and *Configuration Set*.

## Self-hosted, and what follows from it

Running SESKit yourself is the point, and it has consequences that shape
everything else:

- **Nothing phones home.** Chart.js is vendored rather than loaded from a CDN;
  search on this site is client-side rather than Algolia. A self-hosted product
  that breaks when someone else's service is unreachable argues against itself.
- **Your data stays in your account.** Message content and recipients live in
  your PostgreSQL, in your infrastructure. For teams with data-residency
  obligations that is the whole reason to choose this.
- **Operational docs are part of the product.** A hosted service never has to
  tell you how to back it up or upgrade it. See
  [Operating SESKit](../operating/deploying.md).

## Reading on

- [Security model](security-model.md) — credentials, signatures, and the
  boundaries.
- [Prior art](prior-art.md) — what was learned from reading comparable
  projects, and the requirements it generated.
- [Design brief](brief.md) and
  [design system](system.md) — how the dashboard is meant to look
  and why.
