# Contributing to SESKit

Thanks for taking an interest. This document covers everything you need to get a
change merged: the setup, the checks, the commit convention, and the handful of
traps that have already cost someone an afternoon.

> **Working with a coding agent?** [`AGENTS.md`](AGENTS.md) is the
> orientation for one — where things are, the rules that are not negotiable,
> and the working method. Everything operational is here, once.

---

## Contents

- [Getting set up](#getting-set-up)
- [The checks](#the-checks)
- [Git hooks](#git-hooks)
- [Commit messages](#commit-messages)
- [Tests](#tests)
- [The documentation site](#the-documentation-site)
- [Local ports](#local-ports)
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
uv run ruff format --check .   # format
uv run mypy .                  # type-check
```

The last three are **the static gate**: they run on every commit through the
hook and need nothing but Python. mypy passes on the whole tree. Pyright
diagnostics reporting `seskit_core.*` as unresolvable are an editor not using
the workspace venv, not a problem to chase.

**Tests need PostgreSQL and Redis running.** Most of the suite is DB-backed, so
`uv run pytest` with nothing up produces a wall of `ConnectionRefusedError`
that looks like your change broke everything. Either start the dependencies:

```bash
docker compose up -d db redis
```

or push a branch and let CI run them — the full suite takes **under two
minutes** there, which is often faster than starting Docker. The suite uses a
dedicated database (`seskit_test`) and Redis index, created and torn down
automatically, so it never touches development data.

A handful of tests read files rather than the database and run anywhere:
`test_ui_polish.py`, `test_design_system.py`, `test_configuration_docs.py`,
`test_commit_msg.py`, `test_dockerfiles.py`, `test_entrypoint.py`.

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
`provider-ses`, `provider-smtp`, `sdk`, `migrations`, `docker`, `ci`, `deps`,
`docs`, `release`. Omit the scope for repo-wide changes.

Checking a whole series before pushing it:

```bash
for sha in $(git rev-list origin/main..HEAD); do
  git log -1 --format=%B "$sha" > /tmp/cm.txt
  python scripts/check_commit_msg.py /tmp/cm.txt
done
```

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

The fixtures, and which to reach for:

| Fixture | Gives you | Use when |
|---|---|---|
| `client` | The app with a mocked session and Redis | Routing, validation, auth refusals — anything with no persistence |
| `app_client` | The app against **real** Postgres and Redis | Anything that stores or reads a row |
| `signed_in_client` | `app_client` holding a real session cookie | Any dashboard page — they are unreachable signed out |
| `db_session` | A session in a transaction rolled back per test | Setting up or asserting on rows directly |
| `session_factory` | For code that opens its own session | Worker paths |
| `redis_client` | Real Redis on a dedicated db index, flushed per test | Rate limits, caches, markers |
| `queue`, `provider_factory`, `provisioner_factory`, `destination_resolver` | Fakes | Avoiding AWS and outbound HTTP |

Three conventions that keep getting rediscovered the hard way:

- **Docstrings say why the test exists**, not what it does. The line worth
  writing is the failure it prevents.
- **The local environment permits private addresses.** A test wanting a refused
  webhook URL must use a scheme refused everywhere (`ftp://`), not
  `http://127.0.0.1` — loopback is allowed on purpose so a developer can point
  a webhook at their own machine.
- **Jinja autoescapes.** Asserting on a string containing an apostrophe fails,
  because `'` renders as `&#39;`.

---

## The documentation site

```bash
uv run --group docs mkdocs serve            # local, :8000
uv run --group docs mkdocs build --strict   # what CI runs
```

`--strict` promotes a broken internal link to a build failure. It does **not**
check heading anchors, so a renamed heading breaks cross-page `#links`
silently — check those by hand when you rename one.

`mkdocs` and `mkdocs-material` are pinned below their next major on purpose:
Material's own analysis of the MkDocs 2.0 rewrite says it removes the plugin
system with no migration path.

The site deploys to GitHub Pages from `main`, with Pages set to **GitHub
Actions** as the source. Setting it to "Deploy from a branch" makes the deploy
job 404.

---

## Local ports

Deliberately unusual, and the reason is worth knowing:

| | |
|---|---|
| PostgreSQL | **55432** |
| Redis | **56379** |
| API | 8000 |
| Mailpit | 8025 (inbox), 1025 (SMTP) |

Machines with PostgreSQL installed often already have clusters on 5432 *and*
5433. Those bind before Docker does, and the container then looks healthy while
every connection quietly reaches the wrong database. Inside Compose the
services still use the standard ports; only the host side is moved.

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
- **AWS credentials belong to a project, not to the instance.** An access
  key is pasted into the dashboard, checked against AWS before it is stored,
  and kept encrypted under a key derived from `SECRET_KEY`. There is
  deliberately no instance-wide setting for one, and the secret is never
  rendered back out — see `docs/design/security-model.md`.
- **No Node.js.** No npm, no `node_modules`, no JavaScript build step, no
  separate frontend service. A self-hoster runs one Python service, and
  anything requiring a Node toolchain — documentation generators, CSS
  frameworks, component libraries — is out by definition.
- **Do not restyle per page.** The dashboard has a component layer in
  `apps/api/src/seskit_api/templates/components/ui.html` and tokens in
  `static/css/app.css`; a test holds the stylesheet to them. Read
  `docs/design/system.md` before touching any page.

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
