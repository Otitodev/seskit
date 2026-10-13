# Receive email

SESKit can receive mail as well as send it. Mail sent to a domain you choose is
stored, shown in the dashboard's **Inbox**, readable through the
[API](../reference/api.md#get-v1inbound) and the [Python
SDK](../reference/sdk.md#received-mail), and announced to your application as an
[`email.received` webhook](../reference/events.md#emailreceived).

It is built on Amazon SES's own receiving. SES accepts the message, stores the
original in an S3 bucket in **your** account, and announces it; SESKit reads the
announcement, parses the message, and keeps what it finds. There is no mail
server to run and nothing for you to monitor beyond the worker SESKit already
needs.

!!! note "What this is not"
    It does not give you a mailbox. There is no IMAP, no POP and no webmail:
    an application reads the mail through the API or a webhook, and a person
    reads it in the Inbox. To reply, send a message with the
    [send API](../reference/api.md) and set `reply_to`.

## Before you start

- A **verified domain** — see [verify a sender](verify-a-sender.md). Receiving is
  per domain; an email address verifies a sender and has no MX record to point
  anywhere.
- An **AWS connection** in a region where Amazon SES can receive mail. That is
  most regions but not all; the Domains page tells you if yours is one that
  cannot, and which can.
- The third [IAM policy](iam-policies.md#receiving-mail-sixteen-more-optional).
  Receiving creates a bucket and edits SES receipt rules, which nothing else in
  SESKit does, so it is a separate grant you can leave out.
- The worker running, reading from SQS. That is the default
  (`EVENT_INGESTION=sqs`); an instance set to `https` only cannot receive and the
  page says so.

## Turn it on

Open the **Domains** page, find the domain, and press **Start receiving**.

Before you press it the card names what it will create, in your AWS account:

| Resource | Purpose |
|---|---|
| S3 bucket `seskit-inbound-<account>-<region>` | holds the original messages; private, encrypted, reachable over TLS only |
| SNS topic and SQS queue `seskit-inbound` | SES announces a message here; the worker reads it |
| One SES receipt rule for the domain | stores the message and announces it |

**SESKit never replaces your SES receipt rule set.** An account has one active
rule set, and replacing it would switch off whatever you receive now. The rule
is added to the set that is active, at the front, and it has no *stop* action, so
it does not change what any of your other rules do — a message that matches both
simply gets both. If your account has no active rule set, SESKit creates one
called `seskit-inbound` and activates it. Turning receiving off removes only what
SESKit created.

AWS bills these to your account. SESKit does not estimate the amount; see the
Amazon SES, S3, SNS and SQS pricing pages.

## Add the MX record

Turning it on is not enough — mail only arrives once the domain's DNS points at
SES. The card shows the record, with a Copy on each value:

| Type | Name | Priority | Value |
|---|---|---|---|
| `MX` | your domain | `10` | `inbound-smtp.<region>.amazonaws.com` |

The region is the one the domain is verified in. A record naming a different
one would deliver nowhere, and nothing would say so.

!!! warning "This replaces wherever the domain receives mail now"
    An MX record sends **all** of a domain's mail to Amazon SES. If
    `example.com` receives mail somewhere else today — Google Workspace,
    Microsoft 365, a forwarding service — that mail stops arriving there the
    moment this record is live.

    To keep that, receive on a **subdomain** such as `mail.example.com`. Add it on
    the Domains page as its own domain, verify it, and turn receiving on there.
    Mail to `you@example.com` carries on as before; mail to
    `anything@mail.example.com` comes to SESKit.

SESKit cannot check that the record is in place, and neither can the page: DNS is
yours. `dig MX example.com` from any terminal shows what the world sees.

```bash
dig +short MX example.com
```

## Check it works

Send a message to any address at the domain from an ordinary mailbox. It should
appear in the **Inbox** within about a minute — that is how often the worker
looks.

If it does not:

| Symptom | Usually |
|---|---|
| Nothing arrives | The MX record is not live yet (DNS can take a while), or names the wrong region. Check with `dig`. |
| "Start receiving" is greyed out | The card says why: not verified, no AWS connection, a region that cannot receive, an instance set to `https` ingestion, or the domain already receives for another project. |
| Starting fails with a permission error | The message names the AWS action that is missing. Add it to the [policy](iam-policies.md#receiving-mail-sixteen-more-optional). |
| A message shows but its attachments will not download | The original has passed [retention](#retention), or the access key lacks `s3:GetObject` or `s3:ListBucket`. The page says which. |
| "SESKit could not read this message" | The message is stored and counted but nothing could be parsed from it. The original is still downloadable until retention removes it. |

## Reading it

| Where | How |
|---|---|
| **Dashboard** | The Inbox page. Each message shows who it is from and to, who it was actually delivered to, what SES concluded about it, its text and HTML, its attachments, its headers and the original. |
| **API** | [`GET /v1/inbound`](../reference/api.md#get-v1inbound) lists, [`GET /v1/inbound/{id}`](../reference/api.md#get-v1inboundinbound_id) reads one, and attachments and the original download from the same path. |
| **SDK** | `client.inbound.list()`, `.get()`, `.attachment()` and `.raw()`. |
| **Webhook** | An [`email.received`](../reference/events.md#emailreceived) event for every message. It describes the message and does not carry it; fetch the rest with the API. |

```python
page = client.inbound.list(domain="mail.example.com")
for summary in page:
    message = client.inbound.get(summary.id)
    print(message.from_, message.subject, message.verdicts.spf)
```

## Treat all of it as hostile

Everything in a received message was written by somebody who may mean you harm.
SESKit is built accordingly, and your own code should be too.

- **The HTML is shown in a sandbox.** The Inbox renders it as a separate document
  with no script, no forms and no network access, so remote images and tracking
  pixels do not load. A link you click opens inside that frame, still without
  script. The source is one click away in a collapsed card.
- **The API returns HTML exactly as sent.** If your own code renders it, do not
  render it in a page that carries your own credentials.
- **Attachments only ever download.** They are served as opaque bytes whatever
  type the sender declared, with headers that stop a browser from displaying them.
- **The sender's name and address prove nothing.** Anybody can write anything in a
  `From` header. SES checks SPF, DKIM, DMARC, spam and viruses and SESKit shows
  the result, but acts on none of it: whether to trust a message is your
  decision. A check shown as **Not checked** means SES did not say, which is not
  the same as a pass.

See the [security model](../design/security-model.md#received-mail-is-hostile-input)
for the reasoning.

## Retention

The original message and its attachments stay in your bucket for
`INBOUND_RETENTION_DAYS` days — **30** by default, set in
[configuration](../reference/configuration.md#received-mail) — and are then
removed by S3.

The parsed message is not removed. After retention it is still in the Inbox and
the API with its bodies and headers; only the attachments and the downloadable
original are gone. Changing the setting applies the next time receiving is set up,
and messages already stored keep the clock they had.

## Turn it off

Press **Stop receiving** on the domain, then remove the MX record from your DNS.

SESKit removes the receipt rule and, if no other domain uses them, the topic and
queue. **Mail already received is kept.** A bucket that still holds messages is
left in place for the same reason, and removed by retention in time; the card
does not delete somebody's mail as a side effect of switching receiving off.

Removing a domain, or disconnecting AWS, does the same first, so neither can
leave a rule behind in your account.

## Limits

- **One project receives for a domain.** A domain's MX record names a single
  endpoint in a single region, so two projects both receiving for it could not both
  be right. A second is refused, and the page says so without saying whose it is.
- **Messages up to 40 MB**, which is Amazon SES's own limit for storing a message.
- **No quarantine.** SES scans for spam and viruses and SESKit records the verdicts,
  but a flagged message is stored and shown like any other.
- **No threading yet.** A reply carries `In-Reply-To` and `References` and SESKit
  keeps them, but it does not link a reply to the message you sent.
