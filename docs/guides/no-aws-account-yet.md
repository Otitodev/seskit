# No AWS account yet?

You do not need one to try SESKit. [Your first email](../getting-started/first-email.md)
goes to a local Mailpit inbox and needs nothing from Amazon.

You need one when you want mail to reach real people. This page is the short
path there.

## The ladder

Each rung is a real success, and you can stop at any of them.

| | Step | Time | What you can do |
|---|---|---|---|
| **0** | Mailpit, no AWS | 60 seconds | Watch a message go through the whole pipeline |
| **1** | A verified email address | ~5 minutes | Send real mail to verified recipients |
| **2** | A verified domain | Minutes to 72h | Send as anything on your domain |
| **3** | Production access | ~24 hours | Send to anyone |

Most people should do 0 and 1 on the same afternoon, then start 2 and 3 and go
do something else while they wait.

## Creating the account

1. Sign up at [aws.amazon.com](https://aws.amazon.com/). It needs a payment
   card and a phone number; the card is verified with a small temporary charge.
2. Choose a region close to your users and **stay in it** — SES identities,
   queues and configuration sets are per region, and mixing them up is the most
   common way to end up confused about why nothing arrives.
3. Open the SES console once to confirm it is available in that region.

!!! tip "SES is not free, but it is close"
    Roughly \$0.10 per thousand emails, plus a few cents for the SNS and SQS
    traffic delivery events use. The free tier covers 3,000 message-charges a
    month for the first year.

    A test instance sending a few hundred messages costs pennies. This is the
    cost advantage the whole project is built on.

## Credentials for SESKit

SESKit [never stores credentials](connect-aws.md) — it reads them from the
environment it runs in.

For a laptop or a first deployment, an IAM user with an access key is the
simplest thing that works. Create one, attach the
[sending policy](iam-policies.md), and put the key in the environment SESKit
runs in.

!!! warning "Do not use your root account credentials"
    The account you signed up with can do anything, including close the
    account. Create an IAM user, give it only the actions on the
    [IAM policies](iam-policies.md) page, and use that.

In production, an **IAM role** attached to the instance or task is better than
any key: there is no secret to leak, rotate, or accidentally commit. SESKit
picks it up with no configuration at all.

## Then

1. [Connect the account](connect-aws.md) — two read-only calls, creates
   nothing.
2. [Verify a sender](verify-a-sender.md) — start with a single address; it
   needs no DNS.
3. [Leave the sandbox](ses-sandbox.md) — the 24-hour wait, worth starting
   early.
