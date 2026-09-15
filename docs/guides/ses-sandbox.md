# Leave the SES sandbox

Every new AWS account is in the SES sandbox:

| | |
|---|---|
| **200 messages** | per 24 hours |
| **1 message** | per second |
| **Verified recipients only** | you cannot mail arbitrary addresses |

That last one is the restriction people meet first and recognise last. Sending
to a colleague who has not verified their address fails, and the failure looks
like a configuration mistake rather than a policy.

SESKit detects the sandbox on connect and says so on the AWS page until the
account graduates. **If your first real send fails with a rejected recipient,
this is almost always why.**

## Getting out

Request it from the **AWS** page in SESKit. It is a support review, usually
answered within 24 hours, by email.

AWS asks you to confirm that you only mail people who asked for it and that
you have a process for bounces and complaints, and its own docs say a
verified *domain* is what gets a request approved quickly. SESKit checks
those before it lets you ask, so the request it files is one AWS can grant:

| Before requesting | How SESKit knows |
|---|---|
| A verified domain in this region | The Domains page - an address alone is not enough |
| Delivery event reporting set up | The AWS page - this *is* the bounce and complaint process |
| One message sent and delivered | A test send to a verified address, with its delivery event back |

Each shows as a step on the card, ticked when done, and the button is live
once all three are. Then: the kind of mail you send, your website, up to four
contact addresses for AWS's reply, and AWS's acknowledgement to tick. SESKit
fills in the use case for the reviewer from what it knows - that bounces and
complaints come back over SNS and the addresses are
[suppressed](suppression.md), and that every message carries a
[one-click unsubscribe](suppression.md#one-click-unsubscribe) header.

The card then shows the wait, and the outcome when you refresh. If AWS asks a
follow-up question it comes by email and in the AWS Support Center; answer it
there. A declined request says so on the card, with the case to look at, and
you can ask again once you have addressed what AWS wanted.

The access key needs `ses:PutAccountDetails` for this, which the
[documented policy](iam-policies.md) now includes. A key made without it is
told the line to add when the button is pressed.

!!! note "Requested in the console instead?"
    SESKit reads the review state from SES, so a request made in the AWS
    console shows on the card the same way. There is no need to do both.

## Working inside the sandbox

You are not blocked while you wait:

- **Verify your own addresses** and send to those.
- **Use the SES mailbox simulator** — addresses like
  `bounce@simulator.amazonses.com` and `complaint@simulator.amazonses.com` need
  no verification, cost nothing against your reputation, and are the only sane
  way to test that your bounce handling works.
- **Keep using Mailpit** for anything that does not need to leave the building.

The simulator is worth using deliberately. Testing bounce handling by sending
real mail to addresses you hope will fail is how you end up with a bounce rate
that gets you reviewed.

## Quota after graduation

Production access raises the limits but does not remove them. Your quota is
shown on the AWS page and grows as your sending record does — AWS increases it
on its own when you send consistently without high bounce or complaint rates.
