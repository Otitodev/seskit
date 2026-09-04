# IAM policies

SESKit must never ask for `AdministratorAccess`. There are two policies because
they grant genuinely different things, and **you should be able to run SESKit
without the second**.

## Sending — six actions (required)

Everything except delivery events. All of it is scoped to SES, and only one
action can create anything.

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

The first two are read-only and are all that [connecting an
account](connect-aws.md) needs, so you can grant just those to look around
before committing to anything. The identity actions create and remove the
verified senders on the Domains page.

`ses:SendEmail` is the one to grant deliberately: it is the only action here
that can reach the outside world and appear on your AWS bill. You do not need
it to try SESKit — without an AWS connection, sending
[goes to Mailpit](../getting-started/first-email.md) locally.

Removing an identity is the only destructive thing here, and it is guarded:
SESKit deletes the identity in SES only when no other project is still using
it.

## Delivery events — nine more (optional)

!!! caution "Read this one before granting it"
    Everything above is scoped to SES and mostly read-only. This adds
    permission to **create and delete SNS topics and SQS queues** in your
    account — a different kind of trust, and the reason it is a separate policy
    and a separate button rather than something the connect flow quietly needs.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sqs:CreateQueue",
        "sqs:GetQueueAttributes",
        "sqs:SetQueueAttributes",
        "sqs:DeleteQueue",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage"
      ],
      "Resource": "arn:aws:sqs:*:*:seskit-events"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sns:CreateTopic",
        "sns:Subscribe",
        "sns:Unsubscribe",
        "sns:DeleteTopic"
      ],
      "Resource": "arn:aws:sns:*:*:seskit-events"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ses:CreateConfigurationSet",
        "ses:DeleteConfigurationSet",
        "ses:CreateConfigurationSetEventDestination",
        "ses:UpdateConfigurationSetEventDestination",
        "ses:DeleteConfigurationSetEventDestination"
      ],
      "Resource": "*"
    }
  ]
}
```

The SQS and SNS statements are **scoped by resource** to the queue and topic
SESKit creates, so this policy cannot touch anything else you own even by
mistake. If you change `EVENT_RESOURCE_PREFIX`, change those ARNs to match.

The delete permissions are there so that removing event reporting, or
disconnecting the account, actually cleans up. Granting create without delete
would leave SESKit able to make resources in your account and unable to tidy
them away.

`sqs:SendMessage` is deliberately absent. SESKit never writes to the queue —
SNS does, under a queue policy SESKit sets during setup that admits that one
topic and nothing else. Adding it here would grant a permission nothing uses.

## Starting smaller

A reasonable order, if you would rather not grant everything at once:

| Stage | Grant | You can then |
|---|---|---|
| Look around | `sts:GetCallerIdentity`, `ses:GetAccount` | Connect, see your quota and sandbox status |
| Verify senders | `+ ses:*EmailIdentity` | Add domains and addresses |
| Send for real | `+ ses:SendEmail` | Deliver through SES |
| See what happened | the second policy | Delivery events and webhooks |

Each stage is usable on its own, and the dashboard says what is missing rather
than failing obscurely.
