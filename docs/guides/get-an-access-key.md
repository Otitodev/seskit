# Get an AWS access key

SESKit sends through **your** Amazon SES account, so it needs a key from it.
This is the whole path: about five minutes in the AWS console, then one paste
into SESKit.

You need an AWS account. If you have not got one,
[start here](no-aws-account-yet.md) instead — it takes about the same again.

## 1. Create a policy

Sign in to the [AWS console](https://console.aws.amazon.com/) and open **IAM**.
It is region-free, so it does not matter which region the console is showing.

**IAM → Policies → Create policy → JSON**, and paste this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "ses:GetAccount",
        "ses:CreateEmailIdentity",
        "ses:GetEmailIdentity",
        "ses:DeleteEmailIdentity",
        "ses:SendEmail"
      ],
      "Resource": "*"
    }
  ]
}
```

Name it `seskit-sending` and create it.

Six actions, and only two of them are needed to connect and look around. What
each one is for, and the nine more that delivery events need, are in
[IAM policies](iam-policies.md). **Do not attach `AdministratorAccess`** — an
access key is a long-lived credential, and this one only needs to send mail.

## 2. Create a user

**IAM → Users → Create user**. Name it `seskit`.

On the permissions step choose **Attach policies directly** and pick
`seskit-sending`.

Leave **Provide user access to the AWS Management Console** unticked. This user
is for a program; a console password on it is a second way in that nobody
needs.

## 3. Create the access key

Open the user → **Security credentials** → **Create access key**.

Choose **Application running outside AWS**. That is what SESKit is: a service
on your own server, talking to AWS over the internet.

!!! warning "The secret is shown once"
    AWS displays the secret access key on this screen and never again. Copy
    both values now, or download the `.csv`. If you lose it, the key is not
    recoverable — you delete it and make another, which costs nothing.

You now have two values:

| | Looks like | Secret? |
|---|---|---|
| **Access key ID** | `AKIAIOSFODNN7EXAMPLE` | No — an identifier |
| **Secret access key** | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` | **Yes** |

## 4. Paste it into SESKit

Open your SESKit dashboard → **AWS**. Choose the region your SES account is in,
paste both values, and press **Connect**.

SESKit checks the key against AWS before storing anything, so a mistyped one is
refused there and then rather than at your first send. On success the page
shows the account id, the region, your sending quota, and whether the account
is still in the [SES sandbox](ses-sandbox.md).

Then [verify a sender](verify-a-sender.md) and you can send.

## Rotating the key

Make a second key on the same IAM user, paste it into **AWS → Replace the
access key**, and delete the old one in IAM once mail is still flowing.

Replacing checks the new key before it stores it, so a bad paste leaves the
working one in place. Do it this way rather than pressing **Disconnect** —
disconnecting also tears down the queue, topic and configuration set that
[delivery events](delivery-events.md) depend on, which has nothing to do with
rotating a key.

## Where the key lives

Stored on the project, encrypted with a key derived from your `SECRET_KEY`.
It is per project, so two projects on one instance can use two different AWS
accounts.

The security model is worth reading before you decide how you feel about that:
[what encryption at rest does and does not protect](../design/security-model.md#aws-credentials).

!!! danger "Rotating `SECRET_KEY` invalidates every stored key"
    They are encrypted with a key derived from it. Change it and every project
    must be connected again. See [upgrading](../operating/upgrading.md).
