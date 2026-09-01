# Contributing to SESKit

Thanks for taking an interest. This document covers everything you need to get a
change merged: the setup, the checks, the commit convention, and the handful of
traps that have already cost someone an afternoon.

---

## Contents

- [Getting set up](#getting-set-up)
- [The checks](#the-checks)
- [Git hooks](#git-hooks)
- [Commit messages](#commit-messages)
- [Tests](#tests)
- [Docker traps](#docker-traps)
- [Architecture rules](#architecture-rules)
- [Opening a pull request](#opening-a-pull-request)

---

## Getting set up

SESKit uses [uv](https://docs.astral.sh/uv/) workspaces. There is no Node
toolchain and no JavaScript build step.

```bash
git clone https://github.com/Otitodev/seskit.git && cd seskit
cp .env.example .env
uv sync

docker compose up -d db redis mailpit      # dependencies only
uv run alembic upgrade head
```

Then run the two processes in separate shells:

```bash
uv run uvicorn seskit_api.main:app --reload
uv run arq seskit_worker.main.WorkerSettings
```

Or run everything in containers with `docker compose up`.

---

## The checks

All four must pass. CI runs the same ones.

```bash
uv run pytest                  # tests
uv run ruff check .            # lint
uv run ruff format .           # format
uv run mypy .                  # type-check
```

Tests need PostgreSQL and Redis running. They use a dedicated database
(`seskit_test`) and Redis index, created and torn down automatically, so they
never touch development data.

---

## Git hooks

One command, once per clone:

```bash
git config core.hooksPath .githooks
git config commit.template .gitmessage    # optional, but recommended
```

That installs two checks:

- **`pre-commit`** — runs ruff, mypy, and the hygiene checks from
  `.pre-commit-config.yaml`.
- **`commit-msg`** — enforces the commit convention below.

Use `core.hooksPath`, not `pre-commit install`: pre-commit refuses to install
while `core.hooksPath` is set, and going around it would bypass the chaining
described next.

> [!NOTE]
> **Your global hooks keep working.** Git runs exactly one hook per event, so
> pointing `core.hooksPath` at this repo would normally disable anything you
> have configured globally — silently. Both hooks in `.githooks/` therefore run
> your global hook of the same name first, and only then the SESKit check.

Skipping a hook in an emergency: `git commit --no-verify`. CI runs the same
checks, so it will still be caught.

---

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>

Why:  ...
What: ...

Refs: SESKit_MVP.md §12
```

```text
feat(api): add idempotency key support
fix(worker): retry webhook delivery on connection reset
docs: explain the SES sandbox in the quickstart
feat(api)!: drop the v0 send endpoint          # ! marks a breaking change
```

Subject lines are imperative ("add", not "added"), lower case (acronyms like
SES and DKIM are fine), no trailing period, 72 characters max. **The body should
explain why** — the diff already shows what.

Scopes match the repository layout: `api`, `ui`, `worker`, `core`,
`provider-ses`, `sdk`, `migrations`, `docker`, `ci`, `deps`, `docs`, `release`.
Omit the scope for repo-wide changes.

Blocked by the hook? Your message is kept in `.git/COMMIT_EDITMSG` — reopen it
with `git commit -eF .git/COMMIT_EDITMSG`.

Full reference, including troubleshooting and how the hook chaining works:
[`docs/commit-conventions.md`](docs/commit-conventions.md).

---

## Tests

Two kinds, deliberately:

- **Stubbed** (`client` fixture) — database and Redis are mocks. Fast, and lets
  a dependency be made to fail on demand so error paths can be exercised.
- **Real** (`db_session`, `app_client`) — anything touching persistence. A
  unique constraint or a cascade delete cannot be tested against a mock.

Each real test runs inside a transaction that is rolled back afterwards, so
tests share one database without leaking state into each other.

**Name tests after the behaviour, not the function.** A test called
`test_the_same_notification_twice_records_one_event` says what breaks when it
fails; `test_ingest_event_2` does not.

**No test may reach real AWS.** The `app_client` fixture substitutes fake
providers and provisioners by default, so a test cannot reach an AWS account by
forgetting to override something. Where a mock is inadequate — moto does not
implement several SESv2 calls — the gap is recorded in the test module's
docstring with a canary test that fails when the mock catches up.

---

## Docker traps

Two failure modes that look like something else. Both cost real time already.

**Adding a workspace package** means registering it in `docker/Dockerfile` too.
The image copies each member's manifest by hand to keep the dependency layer
cached, and a package missing from that list fails at container start rather
than at build time.

**Adding a dependency to an existing package** needs the anonymous volume
renewed. Compose mounts one at `/app/.venv` so bind-mounted sources do not
shadow the installed environment — and it survives `docker compose build`, so a
rebuilt image still starts with the old `.venv`. The symptom is a
`ModuleNotFoundError` for a package that is provably present in the image:

```bash
docker compose up -d --renew-anon-volumes
```

---

## Architecture rules

A few boundaries the codebase holds to. Breaking one will come up in review.

- **`core` never imports a provider.** It defines the provider interface
  (`Protocol`s in `seskit_core.providers`) and decides *which* provider to use
  by name; the app layer maps that name onto an adapter. The dependency points
  one way only.
- **Provider vocabulary stops at the adapter.** No boto3 response dict, and no
  `ClientError`, escapes `packages/provider-*`. What crosses the boundary is
  core's dataclasses and a normalised `APIError`.
- **Business logic lives in `services`, not in route handlers**, so it can be
  tested without HTTP and reused from a CLI later.
- **Never log message bodies, recipients, or subjects.** Ids and statuses only.
- **AWS credentials are never stored, never accepted in a form, and never
  named in settings.** boto3 resolves them from the environment.

---

## Opening a pull request

1. Branch from `main`.
2. Make the four checks pass locally.
3. Write the commit message body as an explanation, not a summary.
4. If the change alters behaviour someone relies on, update the README or
   `docs/` in the same PR.

If you are adding something substantial, open an issue first so the design can
be agreed before you write it — that is cheaper for everyone than a review that
asks for a different shape.
