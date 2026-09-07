# Suppression

A suppression list is the set of addresses your project will not send to, no
matter what your application asks for.

It exists because of one number. Amazon reviews accounts whose bounce rate goes
over 5%, and a dead address bounces every single time you send to it. Without a
list, a signup typo entered once keeps costing you reputation for as long as
your application keeps retrying it — and the fix is not to remember harder, it
is to stop asking.

SESKit adds addresses to the list on its own, refuses sends to them, and gives
you a page to take one off again.

## What gets added, and what does not

| Event | Added? |
|---|---|
| Hard bounce — `bounce_type` is `Permanent` | **Yes** |
| Complaint — the recipient marked it as spam | **Yes** |
| Soft bounce — `bounce_type` is `Transient` | No |
| `Undetermined` bounce | No |

**The two "no" rows are the important ones.** A transient bounce is a full
mailbox or a greylist; the address works again tomorrow. Suppressing it would
delete a real recipient for good, a few at a time, silently — and nobody
connects "our mail stopped arriving" to a rule that fired weeks ago. Only
`Permanent` is evidence that an address is dead.

See [bounce types](../reference/events.md#bounce-types) for what SES means by
each.

## What happens when you send to one

The send is refused before it reaches SES:

```json
{
  "error": {
    "type": "suppressed_recipient",
    "message": "user@example.com is on this project's suppression list, so nothing was sent. An address lands there after a hard bounce or a complaint, when the recipient unsubscribes, or by hand. The Suppressions page says which, and can take it off if you believe mail can be delivered there again."
  }
}
```

HTTP 422. `to`, `cc` and `bcc` are all checked, and the **whole request** fails
rather than the suppressed recipients being dropped — sending to the rest would
need a second response shape saying who was left out, and a caller who did not
read it would believe everyone got the message.

**Refusing before SES is the whole point.** A message SES never sees cannot
bounce, so it cannot count against the rate that gets accounts reviewed. A
message you send anyway is one you already know will fail.

Addresses are compared with the display name stripped and the case folded, so
`Bob <BOB@Example.com>` and `bob@example.com` are the same address to the list.
Adding a display name is not a way past it.

## The list is per project

SES has its own account-level suppression list. SESKit does not use it, and
does not write to it.

The reason is scope. Your AWS credentials resolve to one account, so an
account-level list is shared by every project on this instance: a marketing
project's complaint would silence a transactional project's password resets to
the same person. That is almost never what anyone wants, and it cannot be
undone per project because the list has no concept of one.

The tradeoff, stated plainly: mail you send through SES from something that is
not SESKit is not filtered by this list.

## Removing an address

**Suppressions → Allow again**, on the dashboard.

Do it when you know why the address bounced and know it is fixed — the mailbox
was recreated, the domain's DNS was broken and is not any more, someone asked
to be put back on. Do not do it to make a number look better: the address will
bounce again and the rate will come back.

Removal is soft. The row stays with the date it was cleared, so "bounced in
March, cleared in April, complained in June" is a history you can still read.

## Adding an address by hand

**Suppressions → Suppress an address.**

For the case automation cannot cover: somebody writes in and asks to be left
alone, and there is no bounce or complaint to record it. The note field is
worth filling in — the next person to read the row will want to know why, and
"asked us to stop, ticket #4412" answers that in a way a date cannot.

## One-click unsubscribe

Messages carry the headers that put an **Unsubscribe** button next to your name
in Gmail and Outlook:

```text
List-Unsubscribe: <https://mail.example.com/u/eyJ...>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

This is worth more than politeness. A recipient who cannot find the unsubscribe
button presses *Report spam* instead, and a complaint costs a reputation point
that an unsubscribe does not.

Pressing it adds the address to this list, with reason `unsubscribe`.

### What it needs

**`PUBLIC_BASE_URL` must be set**, because the link has to be reachable from
the recipient's mail client. With it unset the headers are left off the message
entirely — a button pointing at `localhost` is worse than no button, since it
fails in front of the person whose next move is to report you.

**The message must have exactly one recipient.** A message carries one
`List-Unsubscribe` header and the link names one address, so with two
recipients the link would let either of them unsubscribe whichever one it
happened to name. A blind copy counts as a second person. Send separate
messages if you need per-recipient unsubscribe.

### How the link is protected

The link carries the message id, the address, and an HMAC signature over both,
keyed per project. Editing the address in the URL invalidates the signature, so
nobody can unsubscribe anybody by guessing.

Opening the link does not unsubscribe anyone — it shows a button. Mail clients
and security scanners fetch links in messages without being asked, and a link
that acted on sight would unsubscribe people who pressed nothing. The
unsubscribe happens on the POST, which is what the one-click header tells
providers to send.

There is deliberately **no expiry**. A link in a message somebody kept for two
years should still work; an expired unsubscribe link produces exactly the
complaint the header exists to prevent.

Unknown and altered links both answer `200` with the same sentence, so the URL
cannot be used to find out whether an address or a message exists.

## Hearing about it in your own application

A suppression caused by a message emits an
[`email.suppressed`](../reference/events.md) event to your
[webhooks](webhooks.md):

```json
{
  "type": "email.suppressed",
  "data": {
    "to": ["user@example.com"],
    "reason": "bounce",
    "caused_by": "evt_01J8XQ..."
  }
}
```

| `reason` | Emits an event? |
|---|---|
| `bounce` | Yes — a hard bounce |
| `complaint` | Yes — marked as spam |
| `unsubscribe` | Yes — the recipient used the one-click link |
| `manual` | **No** — somebody added it on the dashboard |

`caused_by` is the event that led to it, or `null` when there was none — an
unsubscribe is the recipient telling SESKit directly.

`manual` is the exception because an event hangs off a message, and an address
somebody typed into the dashboard has no message behind it. If you keep your
own mailing list in step with this one, the dashboard is the one path that will
not tell you about itself.

Consume this if you keep your own mailing list: it is how you find out that an
address left, and it covers every route a message can put one there.

## What is not built yet

- **No HTTP API for the list.** It is the dashboard and the `email.suppressed`
  event today. There is no `GET /v1/suppressions` to poll and no bulk import.
- **Suppressions do not expire.** An address stays until somebody removes it.
