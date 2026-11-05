# The SESKit skill

A skill is a small folder of instructions a coding agent loads when it needs
them. This one teaches an agent how to set up and use SESKit, so it does not
have to guess.

It covers both starting points:

- **From scratch:** install SESKit, connect AWS, verify a sender, send a first
  email.
- **A running instance:** send from an app, set up webhooks, upgrade, back up,
  and fix what is not working.

It also carries the rules that are easy to break by accident: AWS keys go in the
dashboard and never in `.env`, `SECRET_KEY` is never changed, and the steps that
belong to you (the owner account, the AWS key, DNS, TLS) are left to you.

## Install it

It lives in its own repository,
[Otitodev/seskit-skill](https://github.com/Otitodev/seskit-skill). For Claude
Code, copy the folder into your skills directory:

```bash
git clone https://github.com/Otitodev/seskit-skill.git
cp -r seskit-skill/skills/seskit ~/.claude/skills/
```

Or, if you use the [`skills`](https://github.com/vercel-labs/skills) installer:

```bash
npx skills add Otitodev/seskit-skill
```

That tool runs on your machine to place the files. SESKit itself still needs no
Node.

An agent with no skills system can read the file directly:
<https://raw.githubusercontent.com/Otitodev/seskit-skill/main/skills/seskit/SKILL.md>

## Use it

Ask your agent in plain words: "set up SESKit on this server", "send email from
my app through SESKit", "upgrade my SESKit instance". It loads the skill when
the request matches.

## It is a summary

The skill is distilled from these docs and links back to them. If they ever
disagree, these docs are right. For a one-off deploy with no install, the
[copy-paste brief](deploy-with-an-agent.md) does the same job.
