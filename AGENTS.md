# AGENTS.md

Orientation for a coding agent working **on** SESKit.

`CONTRIBUTING.md` is the human version and covers the same ground more gently;
this is the compressed one, written for whoever arrives with no memory of the
last session. If the two ever disagree, `CONTRIBUTING.md` wins.

If you are writing an application that *uses* SESKit, you want
[the documentation](https://otitodev.github.io/seskit/) instead — this file is
about editing the repository.

---

## The one thing that will waste your time

**The test suite needs PostgreSQL and Redis.** Most of it is DB-backed, so
running `uv run pytest` without the stack up produces a wall of
`ConnectionRefusedError` that looks like your change broke everything.

Two ways through:

```bash
docker compose up -d db redis          # local, needs Docker running
```

or push a branch and let CI run it — the full suite takes **under two minutes**
there, which is often faster than starting Docker.

Tests that read files rather than the database run anywhere:

```bash
uv run pytest tests/test_ui_polish.py tests/test_webhook_signing.py \
              tests/test_webhook_destinations.py tests/test_commit_msg.py
```

## Layout

A **uv workspace**. Members are `apps/*` and `packages/*`.

```text
apps/api/                 FastAPI app, Jinja2 templates, static assets
apps/worker/              ARQ background worker
packages/core/            Config, logging, persistence, shared domain logic
packages/provider-aws-ses/  Amazon SES
packages/provider-smtp/   SMTP, for local delivery to Mailpit
packages/sdk-python/      The `seskit` package on PyPI (a stub until Phase 12)
migrations/               Alembic
docs/                     The documentation site (MkDocs)
scripts/                  Repository tooling
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends on
the other. **`core` defines the provider interface and chooses an
implementation, but imports neither provider package** — the dependency only
ever points one way. Provider-specific code that leaks into `core` or the API
is a review failure, not a style preference.

## Rules that are not negotiable

**No Node.js.** No npm, no `node_modules`, no JavaScript build step, no
separate frontend service. This is positioning, not taste (spec §5): a
self-hoster runs one Python service. Anything requiring a Node toolchain is out
by definition — that includes documentation generators, CSS frameworks and
component libraries.

**No credentials in the database.** AWS credentials are resolved by boto3 from
the environment (§9). There is deliberately no setting for them. API keys are
stored as SHA-256 hashes. Webhook signing secrets are the one plaintext secret,
because receivers must read them back to verify signatures.

**Never `AdministratorAccess`.** The IAM policies SESKit asks for are
enumerated in `docs/guides/iam-policies.md` and are scoped by resource where
AWS allows it.

**Do not restyle per page** (§31). The dashboard has a component layer in
`apps/api/src/seskit_api/templates/components/ui.html` and tokens in
`static/css/app.css`. Read `docs/design/system.md` before touching any page. A
change that alters one page's appearance and no component has drifted into
redecoration.

## The static gate

Runs on every commit via the hook, and again in CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```

mypy is the project's type gate and passes on the whole tree. **Pyright
diagnostics reporting `seskit_core.*` as unresolvable are environment noise** —
an editor not using the workspace venv. Do not chase them.

## Tests

Two tiers, and picking the wrong one is the most common mistake:

| Fixture | Gives you | Use when |
|---|---|---|
| `client` | The app with a mocked session and Redis | Testing routing, validation, auth refusals — anything with no persistence |
| `app_client` | The app against **real** Postgres and Redis | Anything that stores or reads a row |
| `signed_in_client` | `app_client` holding a real session cookie | Any dashboard page — they are unreachable signed out |
| `db_session` | A session in a transaction rolled back per test | Setting up or asserting on rows directly |
| `session_factory` | For code that opens its own session | Worker paths |
| `redis_client` | Real Redis on a dedicated db index, flushed per test | Rate limits, caches, markers |
| `queue`, `provider_factory`, `provisioner_factory`, `destination_resolver` | Fakes | Avoiding AWS and outbound HTTP |

Conventions worth copying rather than reinventing:

- **Test names are sentences.** `test_a_refused_url_is_explained_on_the_form`,
  not `test_create_webhook_400`.
- **Docstrings say why the test exists**, not what it does. The line worth
  writing is the failure it prevents.
- **The local environment permits private addresses.** A test wanting a refused
  webhook URL must use a scheme refused everywhere (`ftp://`), not
  `http://127.0.0.1` — loopback is allowed on purpose so a developer can point
  a webhook at their own machine.
- **Jinja autoescapes.** Asserting on a string containing an apostrophe will
  fail, because `'` renders as `&#39;`.

## Commits

Conventional Commits, enforced by `scripts/check_commit_msg.py` on the
`commit-msg` hook and again in CI on pull requests.

```text
<type>(<scope>): <subject>
```

Types: `feat` `fix` `docs` `refactor` `perf` `test` `build` `ci` `chore`
`revert` `style`. Scopes: `api` `ui` `worker` `core` `provider-ses`
`provider-smtp` `sdk` `migrations` `docker` `ci` `deps` `docs` `release`.

Subject ≤ 72 characters, imperative mood, no trailing period, not sentence
case. Body lines ≤ 100. Run the checker before pushing a series:

```bash
for sha in $(git rev-list origin/main..HEAD); do
  git log -1 --format=%B "$sha" > /tmp/cm.txt
  python scripts/check_commit_msg.py /tmp/cm.txt
done
```

Hooks are installed with `git config core.hooksPath .githooks` — **not**
`pre-commit install`, which refuses to run while `core.hooksPath` is set.

## Working method

**Plan before each phase.** The repository is built in numbered phases (spec
§31, currently at Phase 10). Each one gets a written plan, approved before code
is written.

**Branch, do not push to `main`.** Open a pull request: CI runs the full suite
against real Postgres and Redis, which is the only place most tests execute.

**Do not start Docker Desktop unprompted.** It is heavy on the maintainer's
machine. Run the static gate, and either ask or use CI for DB-backed checks.

A useful trick for dashboard visual checks that need neither: render the
template standalone with Jinja, stub `url_for`, serve it beside the real
stylesheet, and look at it. Anything that is template plus CSS rather than real
data can be verified this way with no database at all.

## The documentation site

```bash
uv run --group docs mkdocs serve            # local, :8000
uv run --group docs mkdocs build --strict   # what CI runs
```

`--strict` promotes a broken internal link to a build failure. `mkdocs` and
`mkdocs-material` are pinned below their next major on purpose — Material's own
analysis of the MkDocs 2.0 rewrite says it removes the plugin system with no
migration path.

Deploys to GitHub Pages from `main`, with Pages set to **GitHub Actions** as
the source. Setting it to "Deploy from a branch" makes the deploy job 404.

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
every connection quietly reaches the wrong database.

## Where the reasoning lives

Read these before proposing a change to the areas they cover:

| | |
|---|---|
| `SESKit_MVP.md` | The specification. §31 is the build order |
| `docs/design/prior-art.md` | What was learned from comparable projects, and the requirements it generated. **AGPL boundary: no code from useSend or Plunk may enter this repository** |
| `docs/design/system.md` | Tokens, components, and the one rule: do not make it look like an admin template |
| `docs/design/security-model.md` | Credentials, signatures, SSRF, and what is not covered yet |
| `docs/commit-conventions.md` | The long version of the commit rules |

Much of the reasoning also lives in docstrings, which are unusually long here
on purpose. A module explaining *why* it does something the way it does is the
argument against someone simplifying it later — treat those as load-bearing.
