# Verify a sender

Amazon SES will not send from an address it has not verified, so before your
first real send you need at least one identity on the **Domains** page.

| | Setup | Time | Sends as |
|---|---|---|---|
| **Email address** | Click a link SES mails you | Minutes | That one address |
| **Domain** | Three CNAME records at your DNS host | Up to 72h | Anything on the domain |

## Start with an address

The address form matters out of proportion to its size: it needs **no DNS and
no registrar access**, so it is the fastest way to reach a real send — worth
doing first even if you intend to use a domain.

SES emails a confirmation link to the address. Click it, and that address can
send. Inside the [sandbox](ses-sandbox.md) it can also *receive*, which is what
makes it useful for testing before production access arrives.

## Then add the domain

A domain identity lets you send as anything on it — `hello@`, `billing@`,
`no-reply@` — without verifying each one.

SESKit shows three CNAME records. Add them at your DNS host and SES verifies
the domain when it sees them, usually within an hour but occasionally up to 72.
The records are DKIM keys: they are what lets a receiving server confirm the
mail genuinely came from you, and they must stay in place permanently.

The form does not ask whether you are adding a domain or an address. A value
containing `@` is an address and anything else is a domain — making you
classify your own input is a question with an obvious answer, and getting it
wrong would be your problem rather than ours.

## Re-checking

SESKit re-checks unverified identities every few hours and verified ones
monthly.

That second one is the subtle half. It notices if a DKIM record is removed long
after setup — during a DNS migration, say — which would otherwise look
perfectly fine right up until a send failed.

## SPF and DMARC

SES handles DKIM through those CNAME records. SPF and DMARC are yours to
publish and SESKit does not manage them, but they materially affect whether
your mail is trusted:

- **SPF** — add Amazon SES to your domain's SPF record, or use a custom MAIL
  FROM subdomain so SPF aligns with your own domain.
- **DMARC** — publish a policy once DKIM is verified. Start at `p=none` and
  read the reports before tightening.

Gmail and Yahoo both require authenticated mail from bulk senders. Even at low
volume, unauthenticated mail lands in spam more often than it reaches anyone.
