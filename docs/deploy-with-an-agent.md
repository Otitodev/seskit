# Deploy with a coding agent

If you already work with Claude Code, Cursor, Codex or anything similar, you
can hand the whole deployment to it. **Copy the block below and paste it in.**

It exists because a coding agent is good at exactly the parts of this that are
tedious — SSHing about, editing a `.env`, reading a compose file — and because
the instructions an agent needs are the same ones a person needs, written once.

Nothing in it is secret. It contains no keys, and it deliberately tells the
agent to ask you for the AWS credentials rather than to go looking for them.

---

```text
Deploy SESKit for me.

SESKit is a self-hosted email platform that sends through Amazon SES.
It is open source and MIT licensed: https://github.com/Otitodev/seskit

WHAT IT IS
Two processes from one codebase, plus PostgreSQL and Redis:
  - the API, which serves /v1 and a server-rendered dashboard
  - the worker, which sends the mail, delivers webhooks and polls for
    delivery events
They never talk to each other directly. The API records a message and puts
a job in Redis; the worker takes it from there. That is why a send answers
"queued" rather than "sent", and why the worker needs no inbound port.

WHAT TO DO
1. Check Docker and the Docker Compose plugin are installed and running.
   If not, tell me - do not install them without asking.
2. git clone https://github.com/Otitodev/seskit.git && cd seskit
3. cp .env.example .env
4. In .env, replace SECRET_KEY=changeme with a long random value. Generate
   it with: openssl rand -base64 32
   Do not print the value to me and do not commit it anywhere.
5. docker compose up -d --wait
   This builds the images, applies migrations through a one-shot "migrate"
   service, and starts everything.
6. Confirm it is healthy:
   docker compose exec api python -m seskit_api.doctor
   Every check should pass except possibly the public URL one.
7. Tell me the dashboard URL.

WHAT NOT TO DO
- Do not ask me for AWS credentials and do not put any in .env. SESKit takes
  an AWS access key through its own dashboard, per project, and stores it
  encrypted. I will paste it in myself.
- Do not expose the database or Redis ports publicly.
- Do not set ALLOW_SIGNUP=true. The first registration claims the instance
  and signup closes behind it, which is what should happen.
- Do not regenerate SECRET_KEY if .env already exists. It derives the key
  that stored AWS credentials are encrypted with, so changing it disconnects
  every project.

IF SOMETHING FAILS
Run the doctor above first - it names the one thing to change.
Troubleshooting: https://otitodev.github.io/seskit/operating/troubleshooting/

AFTER IT IS UP
Tell me to do these myself, and stop:
  - open the dashboard and create the owner account
  - get an AWS access key and paste it into the AWS page
    https://otitodev.github.io/seskit/guides/get-an-access-key/
  - put a TLS terminator in front before sending anything real
    https://otitodev.github.io/seskit/operating/deploying/
```

---

## What it will not do for you

**It will not touch AWS.** SESKit takes an access key through its own
dashboard, and the brief tells the agent to leave that to you. An agent that
went and made IAM users on your behalf would be doing the one part of this
worth doing slowly.

**It will not put SESKit on the internet.** No TLS, no firewall, no DNS. Those
depend on your host and your judgement, and a script that guessed would guess
wrong. See [deploying](operating/deploying.md).

**It will not create your account.** The first registration claims the
instance, so it should be you who makes it.

## If you would rather not

The [one-line installer](getting-started/installation.md#on-a-server) does the
same thing without an agent, and the
[manual path](getting-started/installation.md) does it without either.
