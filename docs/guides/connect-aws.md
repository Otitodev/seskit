# Connect an AWS account

Sending switches from Mailpit to Amazon SES for a project once two things are
true: an AWS account is connected, and the `from` address is covered by a
[verified identity](verify-a-sender.md). **Nothing changes in your code** —
same endpoint, same request.

!!! important "An unverified sender is refused, not quietly delivered locally"
    If a project has connected AWS but the sender is not verified, the send
    fails. Falling back to Mailpit would report success while the message
    reached nobody, which is the worst of both outcomes.

## SESKit never stores your credentials

It resolves them the standard boto3 way, in boto3's own order of precedence:

1. an IAM role attached to the EC2 instance, ECS task, or EKS pod
2. the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
3. a shared credentials file (`~/.aws/credentials`)
4. SSO or workload identity where configured

There is deliberately no setting for them. Naming one would invite credentials
into logs, into config dumps, and into a database — and an IAM role, which is
the right answer in production, has nothing to name.

Give the process credentials by whichever route suits your deployment, then
open **AWS** in the dashboard, choose your SES region, and connect. SESKit asks
AWS who the identity is and what it may do, and records the answer.

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

Removes what SESKit recorded about the connection, and tears down any event
infrastructure it created. Because SESKit never held your credentials, there is
nothing else to revoke — turning off the IAM permissions is a thing you do at
AWS, not here.

If a second project on the same instance shares the region, shared
infrastructure stays until the last one stops using it.
