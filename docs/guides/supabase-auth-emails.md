# Send Supabase auth emails through SESKit

Supabase Auth sends sign-up confirmations, password resets and magic links
itself, from a shared mailer that is meant for development and is tightly
rate limited. Supabase can hand that job to your own code instead. This guide
gives it a small function that passes each email to SESKit, so it is sent from
your domain and shows up in the dashboard, the events and the suppression
list like any other message.

```text
Supabase Auth  →  your Edge Function  →  POST /v1/emails on SESKit  →  Amazon SES
```

!!! caution "Not yet run against a live Supabase project"
    The function is tested on its own: signatures, links, templates, retries and
    the case where SESKit is down. It has not yet been run by SESKit's
    maintainers against a real Supabase project. Do the [test signup](#test-it)
    on a project you can afford to break before you rely on it.

    The function is TypeScript because Supabase Edge Functions run on Deno.
    It runs on Supabase, not on your SESKit server, and SESKit itself needs no
    Node.js.

## Before you start

- **SESKit reachable over HTTPS from the internet.** The function runs on
  Supabase's servers and calls SESKit, so `http://localhost:8000` will not do.
- **A verified sender** — see [verify a sender](verify-a-sender.md). The function
  sends from one address you choose, such as `auth@example.com`.
- **SES out of the sandbox.** In the [sandbox](ses-sandbox.md) SES only delivers
  to verified addresses, so your real users would not get their emails.
- **A SESKit API key made for this function**, so you can revoke it without
  touching anything else.
- The [Supabase CLI](https://supabase.com/docs/guides/cli), logged in and linked
  to your project.

## Add the function

Create it and replace its contents with the code below:

```bash
supabase functions new send-email
```

Save the code as `supabase/functions/send-email/index.ts`. A copy to download is
at [`send-email.ts`](supabase/send-email.ts).

```ts
--8<-- "docs/guides/supabase/send-email.ts"
```

Deploy it **without JWT verification**:

```bash
supabase functions deploy send-email --no-verify-jwt
```

Supabase Auth calls a hook before it has issued the user a token, so there is no
JWT to check. That does not leave the function open: it refuses any request that
does not carry a valid signature from Supabase (see [what it checks](#what-it-checks)).

## Turn the hook on

1. Set the secrets that do not depend on the hook:

    ```bash
    supabase secrets set \
      SESKIT_URL=https://mail.example.com \
      SESKIT_API_KEY=sk_... \
      FROM_ADDRESS=auth@example.com \
      APP_NAME="Your app"
    ```

2. In the Supabase dashboard, open **Authentication → Hooks → Send Email**, choose
   **HTTPS**, and set the URL to
   `https://<project-ref>.supabase.co/functions/v1/send-email`.
3. Generate the secret there. It looks like `v1,whsec_...`. Save the hook, then
   give the function the secret straight away:

    ```bash
    supabase secrets set SEND_EMAIL_HOOK_SECRET='v1,whsec_...'
    ```

Until the last step is done the function answers every request with an error, so
do it in one sitting and not on a busy project.

## Test it

1. Sign up with an address you control, then ask for a password reset and a magic
   link.
2. Each should arrive from your sender within a few seconds. Open SESKit's
   **Emails** page: each appears there, and delivery shows up as it would for any
   other message.
3. If one does not, [look at the function's logs](#when-it-fails).

## What it sends

| Supabase sends | The email | Has |
|---|---|---|
| `signup` | Confirm your email | a link |
| `invite` | You have been invited | a link |
| `magiclink` | Your sign-in link | a link and a code |
| `recovery` | Reset your password | a link |
| `email_change` | Confirm your new email | a link; a **secure** change sends two emails, one to each address |
| `email` | Your sign-in code | a code |
| `reauthentication` | Confirm it is you | a code |

Supabase can also send security notifications (a changed password, a linked
identity and similar). The function has no template for these and skips them:
Auth carries on, and the log says what was skipped. To send one, add an entry to
`COPY` in the code.

The wording is short and plain. Change the text in `COPY` to match your voice; the
HTML is built with every value escaped, so keep it that way if you edit it.

## What it checks

Supabase signs each request with the [Standard Webhooks](https://www.standardwebhooks.com/)
scheme, and the function refuses one that is unsigned, altered, signed with another
secret, or older than five minutes. The signature is the only thing that proves a
request came from your Supabase project, which is why the JWT check can be off.

Keep the hook secret and the SESKit key in Supabase's secrets and nowhere else.
Neither belongs in the code or in your repository.

## When it fails

Supabase allows the whole hook **5 seconds**. The function gives SESKit 3 of them.

| What happened | The function answers | What Supabase does |
|---|---|---|
| SESKit did not answer in time, is down, or is busy | `503` | Retries, up to three times, two seconds apart |
| SESKit refused the message — a wrong key, an unverified sender | `400` | Returns an error to your app; the user sees the sign-up or reset fail |
| The signature is missing or wrong | `401` | Returns an error to your app |

A retry cannot send an email twice. Each one is sent with an `Idempotency-Key`
built from the email's kind, the user, the address and the token, so SESKit
returns the first message and sends nothing more. A second reset request makes a
new token and so sends a new email.

The function's logs, under **Edge Functions → send-email → Logs**, carry a short
event name and a status. They never contain an address, a token or a link.

!!! warning "SESKit is now in the sign-in path"
    If SESKit is down for longer than Supabase's retries, users cannot confirm an
    account or reset a password until it is back. Supabase's own mailer has the
    same dependency on Supabase. Decide whether that trade is right for your app.

## Limits

- **Supabase's own email rate limits.** Supabase's documentation does not say
  whether turning the hook on lifts them. Look at your project's Auth rate-limit
  settings after you enable it.
- **One sender.** Every email comes from `FROM_ADDRESS`.
- **A suppressed address fails the request.** SESKit refuses to send to an address
  on the [suppression list](suppression.md) (`422`), the function answers `400`, and
  the user sees the sign-up or reset fail.
