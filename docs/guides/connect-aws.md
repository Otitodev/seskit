# Connect an AWS account

Sending switches from Mailpit to Amazon SES for a project once two things are
true: an AWS account is connected, and the `from` address is covered by a
[verified identity](verify-a-sender.md). **Nothing changes in your code** —
same endpoint, same request.

!!! important "An unverified sender is refused, not quietly delivered locally"
    If a project has connected AWS but the sender is not verified, the send
    fails. Falling back to Mailpit would report success while the message
    reached nobody, which is the worst of both outcomes.

## Connect it

1. **[Get an access key](get-an-access-key.md)** from your AWS account — about
   five minutes in the IAM console.
2. Open **AWS** in the dashboard, choose the region your SES account is in, and
   paste both values.
3. Press **Connect**.

SESKit checks the key against AWS before storing anything, so a wrong one is
refused there and then rather than at your first send. On success the page
records the account id, the region, your sending quota and your sandbox state.

No shell, no AWS CLI, and nothing to set on the server.

## The key is stored, per project

SESKit keeps the access key on the project, with the secret encrypted under a
key derived from your `SECRET_KEY`.

Per project, which means two projects on one instance can send through two
different AWS accounts. That was not possible when credentials came from the
environment the process ran in — everything on the instance necessarily
resolved the same account.

The trade is real and worth reading before you decide how you feel about it:
SESKit now holds a long-lived AWS credential, so a database dump becomes a
credential leak. What the encryption does and does not protect is set out in
the [security model](../design/security-model.md#aws-credentials).

!!! danger "Rotating `SECRET_KEY` invalidates every stored key"
    Change it and every project must be connected again. There is no way
    around this: the encryption key is derived from it. See
    [upgrading](../operating/upgrading.md).

## Connecting creates nothing

The connect step is two read-only calls — `sts:GetCallerIdentity` and
`ses:GetAccount`. Nothing appears in your AWS account and nothing appears on
your bill.

Setting up [delivery events](delivery-events.md) *does* create things — a
queue, a topic and a configuration set — but that is a separate button, it
tells you what it will create before you press it, and disconnecting removes
them again.

This split is why you can grant only the two read-only permissions and look
around before committing to anything. See [IAM policies](iam-policies.md).

## What gets recorded

The account id, the region, the sending quota, and whether the account is still
in the [SES sandbox](ses-sandbox.md).

These are read when you connect and when you press **Refresh** — not on every
page load, which would put an AWS round trip in the render path and invite
throttling. The page says when it last checked.

## Disconnecting

Removes the stored access key along with everything else SESKit recorded, and
tears down any event infrastructure it created.

**The key still exists in IAM.** Deleting somebody's AWS credential is not
something a Disconnect button should be able to do, so revoking it is a thing
you do at AWS. If you are disconnecting because the key leaked, delete it there
too.

To change a key rather than remove it, use **Replace the access key** — that
leaves delivery events alone.

If a second project on the same instance shares the region, shared
infrastructure stays until the last one stops using it.
