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

[Request production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html)
from the SES console. It is a support review, usually answered within 24 hours.

AWS will ask how you handle bounces and complaints. Answer concretely — that
you receive [delivery events](delivery-events.md), that bounce and complaint
rates are visible to you, and what you do when an address hard-bounces. A
specific answer is approved faster than a reassuring one.

!!! tip "You have a concrete answer to give"
    SESKit maintains a [suppression list](suppression.md): a hard bounce or a
    complaint puts the address on it, and later sends to that address are
    refused before they reach SES. Messages also carry a
    [one-click unsubscribe](suppression.md#one-click-unsubscribe) header. Both
    are worth saying in the request — AWS reviews accounts above **5% bounce**
    and **0.1% complaint**, and asks what you do about them.

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
