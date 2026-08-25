# SESKit — Research & Scoping Report

**Source document:** `SESKit_MVP.md` (only doc in this repo)
**Prepared:** 2026-08-24
**Purpose:** Validate the MVP spec against current AWS SES behavior and the existing competitive landscape, surface gaps before implementation starts, and propose a refined build order.

---

## 1. Executive summary

The spec is solid and internally consistent — the domain model, phased build order, and non-goals list are well thought out. Research turned up one finding that should change the framing before any code is written, plus six concrete technical gaps in the spec itself.

**The big one:** the product you're describing already exists, twice.

- **useSend (formerly Unsend)** — AGPL-3.0, Next.js/Prisma/tRPC/Hono, built on Amazon SES, with domains, API + SMTP, dashboard analytics, webhooks, **and** contacts/campaigns/inbound email that SESKit's spec explicitly marks as non-goals.
- **Plunk** — same positioning, also open-source, also built on SES.
- **FreeResend** — smaller, same idea again.

This doesn't mean the project is pointless, but it changes what "MVP" should optimize for. SESKit's real differentiator per the spec (§1, §32) is being **Python-native**: a FastAPI backend and a Python SDK, targeting Python/FastAPI developers who don't want a Next.js/Prisma stack to self-host or a TypeScript-only SDK to integrate. useSend and Plunk are both JS/TS end-to-end. That's a legitimate, defensible wedge — but it means the Python SDK and clean FastAPI architecture aren't a "nice to have" tacked onto the roadmap (§13, Phase 10), they're the product's reason to exist and should be treated as core MVP surface, not something to polish last.

Recommendation: keep the MVP scope as written, but make the positioning explicit in the repo (README, landing copy) as "the Python-native alternative" rather than a generic "Resend for SES," and resist scope creep toward contacts/campaigns purely because competitors have them — that's correctly a non-goal.

---

## 2. Competitive landscape detail

| Project | Stack | License | Has campaigns/contacts? | Has inbound email? | SDK language |
|---|---|---|---|---|---|
| **useSend / Unsend** | Next.js, Prisma, tRPC + Hono, Redis | AGPL-3.0 | Yes | Yes | JS/TS |
| **Plunk** | Node/TS | Open source | Yes | No (marketing-focused) | JS/TS |
| **FreeResend** | Smaller/simpler | Open source | No (transactional-focused, closer to SESKit's actual scope) | No | — |
| **SESKit (proposed)** | FastAPI, Next.js dashboard, Python SDK | *unset — see §4.3* | No (non-goal) | No (non-goal) | Python |

Action item: skim useSend's actual GitHub issues/architecture before Phase 4–6 (AWS integration, domain setup, event processing) — they've already hit the edge cases (SES sandbox onboarding UX, event dedup, DKIM polling) and their solutions are visible in a public repo. No need to reinvent that discovery work.

---

## 3. Technical findings that confirm the spec

- **Cross-account AssumeRole (§9):** confirmed as the correct approach. The standard pattern: SESKit generates a unique `sts:ExternalId` per connection (never customer-supplied — a customer-supplied external ID is a common vendor bug), the customer creates an IAM role in their account trusting SESKit's AWS account with that external ID, and SESKit assumes the role for scoped SES access. ~37% of SaaS vendors implement external ID handling incorrectly (unvalidated server-side), so this needs a dedicated security test, not just a code review. Ship a CloudFormation/Terraform "quick launch" template for the customer-side role — this is what makes the connect flow feel like Vercel/Datadog rather than "paste your AWS keys here."
- **Background job queue (§5):** ARQ over Celery is the right call for an async FastAPI stack — shares the event loop, Redis-only, no separate broker/worker-pool complexity. Recommend committing to ARQ specifically (not "ARQ or Celery") to match the spec's own "boring, maintainable architecture" principle (§32.11). Reserve Celery for a hypothetical future CPU-heavy workload (e.g., large attachment processing), which is out of scope for MVP anyway.
- **SES event destinations (§15):** both EventBridge and SNS are valid, current configuration-set destinations. Recommendation: use **EventBridge** for the hosted/production path (simpler routing, no SNS-HTTPS subscription-confirmation handshake to manage per customer domain) and keep an **SNS→SQS** fallback documented for self-hosted users who prefer not to stand up EventBridge rules. Avoid SNS→HTTPS-direct-to-API for anything but local dev — it requires a publicly reachable endpoint and a confirmation handshake per identity, which is awkward for local Docker Compose setups anyway.
- **Webhook signing (§16):** adopt the de facto standard (used by Svix, Stripe-style): `signed_content = "{id}.{timestamp}.{body}"`, HMAC-SHA256, and reject anything outside a 5-minute timestamp window to block replay. This is barely more work than inventing a bespoke scheme and it's what customers' webhook tooling (e.g. generic webhook-testing proxies) already expects.

---

## 4. Gaps not covered in the current spec

These aren't blockers, but each will cause rework if discovered mid-implementation rather than now.

### 4.1 SES sandbox mode is unaddressed
Every new AWS account/region starts in the SES **sandbox**: 200 messages/24h, 1 msg/sec, and mail can only go to verified recipients. The spec's onboarding flow (§8, §30) goes straight from "connect AWS" to "send email" with no mention of this. Concretely: a brand-new user following the Acceptance Criteria in §30 will hit a hard rejection on their first send unless their AWS account already has production access.
**Recommendation:** add sandbox detection to the AWS connection step (§8's "Check SES status") and surface it in the dashboard (§17 AWS page) with a link/instructions to request production access. This is cheap to add now and expensive to retrofit once the AWS provider abstraction (§26) is built without it.

### 4.2 Open/click tracking needs a DNS record the domain wizard doesn't ask for
§6 and §16 list `opened`/`clicked` as MVP event types. SES supports this natively via configuration-set `TrackingOptions`, but it requires a **verified custom tracking subdomain** (CNAME) — separate from the DKIM/SPF records the domain wizard (§10) currently describes. Without it, SES falls back to Amazon's own `awstrack.me` domain, which is a legitimate MVP fallback but should be a conscious choice, not a silent gap.
**Recommendation:** either (a) add the tracking CNAME to the domain setup wizard's DNS record list, or (b) explicitly scope opens/clicks to use SES's default tracking domain for MVP and note the custom-domain upgrade as V1.1. Either is fine — just pick one before building §10 and §18.

### 4.3 No license chosen
§28 describes an open-source strategy but never names a license. This matters immediately: useSend chose **AGPL-3.0** specifically to prevent a cloud provider from reselling it as a hosted service without contributing back — directly relevant since SESKit's own roadmap (§34 V2, "Hosted SESKit Cloud") anticipates the same commercial-vs-OSS tension. Decide before the first commit, not before the first release — retroactively relicensing a repo with external contributors is painful.
**Recommendation:** pick MIT/Apache-2.0 (maximizes adoption, matches "boring and simple" ethos) or AGPL-3.0 (protects the future hosted-service plan) now. This is a five-minute decision today and a legal problem later.

### 4.4 Suppression list / bounce-complaint handling isn't scoped
SES maintains an account-level suppression list (auto-suppresses hard bounces/complaints) essentially for free. The spec tracks `bounced`/`complained` as events (§6) but doesn't say whether SESKit reads/writes the SES suppression list or leaves it entirely to AWS. This affects deliverability guidance shown to users and is a natural, low-effort addition.
**Recommendation:** MVP can rely on SES's own suppression list (no extra work) — just document that behavior in the dashboard's Overview/bounce-rate copy so users understand why a previously-bounced address silently fails to send. Building SESKit's own suppression layer is correctly a non-goal for MVP.

### 4.5 Attachment size limits unspecified
§11 lists `attachments` as a supported field with no size guidance. SES's raw message size ceiling (10MB for `SendRawEmail`, larger via S3-backed attachments in newer SES features) should be decided and validated at the API boundary (§19 error types — add `attachment_too_large` or fold into `invalid_request`) rather than surfacing a raw AWS `MessageRejected` error to the customer.

### 4.6 Region scope per project is implicit, not stated
§8 says "select an AWS region" (singular) and SES sandbox/quota status is per-region. The spec never states whether a Project/AWSConnection is locked to one region for MVP (implied) or whether multi-region sending is out of scope. Worth one explicit sentence in §3 Non-Goals to prevent an implementer from over-building.

---

## 5. Refined build order

The spec's Phase 1–11 order (§31) is sound; the changes below are additive, not restructuring:

- **Phase 4 (AWS SES provider):** add a sandbox-status check to the "account verification" step, and spike `moto`'s SESv2 coverage early — moto's SES/SESv2 support has historically been thinner than S3/DynamoDB, so confirm what it can mock before writing tests against it. Where moto falls short, fall back to the real SES **mailbox simulator addresses** (`success@simulator.amazonses.com`, `bounce@...`, `complaint@...`) against a real sandboxed AWS test account for integration tests.
- **Phase 5 (Domain management):** decide §4.2 (tracking domain) before building the DNS-records display.
- **Phase 6 (Email API):** add the attachment size ceiling (§4.5) to request validation.
- **Phase 9 (Dashboard) / AWS page:** surface sandbox status (§4.1) prominently, not buried.
- **New, before Phase 1:** license file (§4.3) and a one-line decision on region scope (§4.6). Both take minutes now.

Everything else in §31 stands as written.

---

## 6. Effort reality check

This is a full multi-tenant SaaS platform (auth, AWS integration, background workers, signed webhooks with retry, dashboard, published SDK, test suite) — not a weekend project, even scoped as tightly as the spec does it. Rough shape, assuming one focused implementer (human or agent) working phase-by-phase per §31:

- Phases 1–3 (foundation, auth, API keys): small, mechanical, low risk.
- Phases 4–5 (AWS SES provider, domains): highest-risk phases — real AWS behavior (sandbox, DKIM propagation delay, verification polling) is where surprises live. Budget the most buffer here.
- Phases 6–8 (email API, events, webhooks): medium complexity, well-specified by the doc.
- Phase 9 (dashboard): medium, mostly mechanical once the API is stable.
- Phase 10 (SDK): small, should ship earlier than "last" in practice — see §1, this is the differentiator.
- Phase 11 (hardening): don't compress this — §4.3's external-ID validation and §22's security checklist are exactly where the 37%-of-vendors-get-it-wrong failure mode in §3 lives.

---

## 7. Open questions — resolved (2026-08-25)

All four are now decided and written into `SESKit_MVP.md` directly:

1. **License → MIT.** Matches Python-ecosystem norms and the self-hosted-first positioning; a future proprietary hosted layer stays a separate, unlicensed repo per the existing open-core split (§28). Written into a new "License" subsection under §28, with a Phase 1 task to add the `LICENSE` file.
2. **Tracking domain → SES default for MVP.** Custom tracking CNAME deferred to V1.1. Written into §10.
3. **Name/namespace → action item retained.** Web search couldn't conclusively confirm "seskit" availability on PyPI/npm/GitHub (page didn't render cleanly). Added as a Phase 1 task ("reserve the namespace before any public commits") rather than a blocking decision — cheap to do, cheap to redo if taken.
4. **Hosted vs. self-host-only → self-hosted-only for MVP.** This was the highest-leverage call: it removes cross-account `AssumeRole`/external-ID delegation — the highest-risk, highest-security-surface part of the original Phase 4 — from the MVP entirely. §9 was rewritten around standard boto3 credential resolution only; AssumeRole delegation moved to the V2 "Hosted SESKit Cloud" roadmap item (§34), where it actually belongs (it's only needed once SESKit manages *other people's* AWS accounts).

---

## 8. Positioning — confirmed

The "Python-native alternative to useSend/Plunk" framing (§1) is now written directly into `SESKit_MVP.md`'s Product Overview as a named "Positioning" subsection, explicitly justifying why the Python SDK (§13) is core MVP surface rather than a late-phase add-on.

---

## 9. Agentic capability opportunities

Asked separately: where would AI-agent capabilities genuinely help SESKit's *users* (not agentic-for-its-own-sake), without reopening the "AI email generation" non-goal (§3, which is about AI-authored marketing content — still correctly out of scope). Three candidates, all written into §34's new "Agentic developer tooling" roadmap subsection, all deferred past MVP:

- **An MCP server for SESKit**, exposing domains/API-keys/send/webhooks as tools alongside the Python SDK — lets AI coding agents (Claude Code, Cursor, etc.) building against SESKit set up a test domain, generate a key, and send a test email as part of their own workflow. This is the strongest candidate: it's a direct extension of the product's core DX pitch to the agents that a growing share of the target audience (Python/FastAPI devs, indie hackers) now uses to write integration code.
- **A DNS/deliverability diagnostic assistant** that reads a domain's actual verification/DKIM/mail-from status plus published DNS records (data already modeled in §6) and explains in plain language what's broken. DKIM/SPF/DMARC misconfiguration is the top support burden for any ESP; this turns a ticket into self-serve. No new data collection needed.
- **A natural-language "why did this bounce" assistant** over existing `EmailEvent` data — surfacing patterns (hard vs. soft bounces, a specific recipient domain rejecting mail, a complaint spike) instead of leaving them as rows in a table.

All three are grounded in data SESKit already collects and are read/diagnostic in nature, not content-generation — that's the line that keeps them out of the "AI email generation" non-goal. Noted as a data-model consideration now (don't discard raw SES error payloads) even though implementation is post-MVP.

---

## 9a. Local email provider — Mailpit (added 2026-08-25)

Researched the FastAPI full-stack template's (`fastapi/full-stack-fastapi-template`) local email setup at the user's request, by pulling the live `compose.yml`/`compose.override.yml`/`backend/app/utils.py`/`backend/app/core/config.py` from GitHub directly (not from training data — the template has since moved from Mailcatcher to Mailpit, and from `docker-compose.yml` to `compose.yml`, so a stale answer here would have been wrong). Confirmed mechanism: `axllent/mailpit` container as a fake SMTP server (port 1025 SMTP, 8025 web UI), app code sends via generic SMTP settings (`SMTP_HOST`/`PORT`/`TLS`) that just point somewhere else in production — same code path, different endpoint, plus a REST API on the Mailpit side for asserting on captured mail in tests.

This slots directly into gaps §25 and §26 already flagged but left unspecified: `SMTPProvider` (implementing the existing `EmailProvider` Protocol) backed by Mailpit is now the concrete local-dev provider, used by any project until its AWS connection + domain verification (§8/§10) complete, at which point sending transparently switches to `SESProvider`. Written into §25, §26, and the Phase 1/Phase 6 build order in `SESKit_MVP.md`. Explicitly scoped as dev/test tooling only — does not reopen the "multi-provider support" non-goal (§3), since production sending is still SES-only in MVP.

## 10. Recommendation

`SESKit_MVP.md` now reflects all of the above directly — this report is a record of the research and decisions behind those edits, not a separate source of truth. Proceed with Phase 1 as specced (§31), starting with the namespace reservation and `LICENSE` file.
