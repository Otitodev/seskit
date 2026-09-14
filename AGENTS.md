# AGENTS.md

Orientation for a coding agent working **on** SESKit — written for whoever
arrives with no memory of the last session.

Everything operational — setup, the checks, hooks, commit format, the test
fixtures, ports, Docker traps — is in [`CONTRIBUTING.md`](CONTRIBUTING.md),
once. This file is what is *not* in there: where things are, the rules that
are not negotiable, how the work is done, and where the reasoning lives. If
the two ever disagree, `CONTRIBUTING.md` wins.

If you are writing an application that *uses* SESKit, you want
[the documentation](https://otitodev.github.io/seskit/) instead.

---

## Before anything else

**The test suite needs PostgreSQL and Redis.** `uv run pytest` with nothing
running produces a wall of `ConnectionRefusedError` that looks like your
change broke everything. `CONTRIBUTING.md` has the two ways through; the
short version is that CI runs the full suite in under two minutes, which is
usually faster than starting Docker.

**Do not start Docker Desktop unprompted.** It is heavy on the maintainer's
machine and has been killed by memory pressure mid-run more than once. Run the
static gate locally; use CI, or ask, for anything DB-backed.

## Layout

A **uv workspace**. Members are `apps/*` and `packages/*`.

```text
apps/api/                   FastAPI app, Jinja2 templates, static assets
apps/worker/                ARQ background worker
packages/core/              Config, logging, persistence, shared domain logic
packages/provider-aws-ses/  Amazon SES
packages/provider-smtp/     SMTP, for local delivery to Mailpit
packages/sdk-python/        The `seskit` package on PyPI
migrations/                 Alembic
docs/                       The documentation site (MkDocs)
docker/                     Dockerfiles and the container entrypoint
scripts/                    Repository tooling
```

`apps/api` and `apps/worker` both depend on `packages/core`; neither depends
on the other. **`core` defines the provider interface and chooses an
implementation, but imports neither provider package** — the dependency only
ever points one way. Provider-specific code that leaks into `core` or the API
is a review failure, not a style preference.

## Rules that are not negotiable

**No Node.js.** No npm, no `node_modules`, no JavaScript build step, no
separate frontend service. This is positioning, not taste (spec §5): a
self-hoster runs one Python service. Anything requiring a Node toolchain is
out by definition — documentation generators, CSS frameworks, component
libraries included.

**AWS credentials belong to a project, not to the instance.** An access key is
pasted into the dashboard, verified against AWS before it is stored, and kept
encrypted under a key derived from `SECRET_KEY` (§9, revised in Phase 14).
There is deliberately no instance-wide setting for one, the secret is never
rendered back out, and `stored_credentials()` in `core/services/credentials.py`
is the only place it is ever decrypted. API keys are SHA-256 hashes. Webhook
signing secrets are the one plaintext secret, because receivers must read them
back to verify signatures.

**Never `AdministratorAccess`.** The IAM policies SESKit asks for are
enumerated in `docs/guides/iam-policies.md` and scoped by resource where AWS
allows it.

**Do not restyle per page** (§31). The dashboard has a component layer in
`apps/api/src/seskit_api/templates/components/ui.html` and tokens in
`static/css/app.css`, and `tests/test_design_system.py` holds the stylesheet
to its own rules. Read `docs/design/system.md` before touching any page.

**Never log message bodies, recipients or subjects.** Ids and statuses only.

## Working method

**Plan before each phase.** The project was built in numbered phases (spec
§31, fourteen so far). Each one gets a written plan, approved before code is
written. Smaller changes still get a stated plan before a first commit.

**One concern per commit, and the commit message is the design record.**
Bodies explain *why*, at length; the diff already says what. Read a few in
`git log` before writing one.

**Branch, open a pull request, wait for CI.** Never merge on the other checks
while the test job is still running — a rebase produces a commit nothing has
tested, and `gh pr merge --auto` merges immediately when auto-merge is not
enabled on the repository. Both have put a broken commit on `main` before.

**A guard that passes on the broken code protects nothing.** When adding a
test for a bug, run it against the code as it was and confirm it fails there.
Several tests in this repository say so in their docstrings; the habit is
worth keeping.

**Verify in the rendered output, not the accessor.** A header that reads back
correctly can still be corrupt on the wire; a template that reads fine can
still 500. Rendering a page offline with Jinja and the real stylesheet needs
no database and has caught what reading the source did not.

**The default for a `Checks` box in a PR is to delete the line**, not to tick
it. A ticked box that is not true is worse than no box.

## Where the reasoning lives

Read these before proposing a change to the areas they cover:

| | |
|---|---|
| `SESKit_MVP.md` | The specification. **Kept locally by the maintainer and not in the repository**; every `§` in the code cites a section of it, and §31 is the build order. Ask if a citation matters for your change |
| `docs/design/prior-art.md` | What was learned from comparable projects and the requirements it generated. **Also local**; the code cites it by name in some thirty docstrings. **AGPL boundary: no code from useSend or Plunk may enter this repository** |
| `docs/design/security-model.md` | Credentials, signatures, SSRF, and what is not covered yet |
| `docs/design/system.md` | Tokens, components, and the one rule: do not make it look like an admin template |
| `docs/commit-conventions.md` | The long version of the commit rules |

Much of the reasoning also lives in docstrings, which are unusually long here
on purpose. A module explaining *why* it does something the way it does is the
argument against someone simplifying it later — treat those as load-bearing.
