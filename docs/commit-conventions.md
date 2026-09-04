# Commit conventions

The full reference for SESKit commit messages, the hooks that enforce them, and
what to do when one blocks you.

`.gitmessage` is the short version — it appears in your editor on every commit.
This document is the long version: the reasoning, the architecture, and the
troubleshooting.

---

## Setup

Once per clone:

```bash
git config core.hooksPath .githooks
git config commit.template .gitmessage    # optional, but recommended
```

That is the whole installation. It wires two hooks:

| Hook | Runs |
|---|---|
| `pre-commit` | ruff, mypy, and the hygiene checks in `.pre-commit-config.yaml` |
| `commit-msg` | the format rules below |

**Do not use `pre-commit install`.** It refuses to run when `core.hooksPath` is
set, and installing around it would bypass `.githooks/` — which is what keeps
your own global hooks working. See [How the hooks work](#how-the-hooks-work).

---

## The format

[Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>

Why:  ...
What: ...

Refs: SESKit_MVP.md §12
```

A real one:

```text
feat(api): add idempotency key support

Why:  Duplicate POST /v1/emails retries were sending the same mail twice.
What: Store Idempotency-Key per project; return the existing Email
      record instead of re-sending.

Refs: SESKit_MVP.md §12
```

More subject lines:

```text
fix(worker): retry webhook delivery on connection reset
docs: explain the SES sandbox in the quickstart
refactor(core): extract the provider protocol
build(docker): pin the postgres image to 16-alpine
chore: reserve the seskit namespace
feat(api)!: drop the v0 send endpoint            # ! marks a breaking change
```

### Why Conventional Commits

Not ceremony for its own sake. Two concrete payoffs for this project:

- **Releases.** The Python SDK ships to PyPI (Phase 12) and the roadmap runs
  through V1.1, V1.2, and V2 (§34). Typed commits let a changelog and a semver
  bump be derived rather than hand-assembled.
- **A monorepo needs scopes.** `git log --oneline -- apps/api` answers "what
  changed in the API", but `git log --oneline --grep '^feat(api)'` answers
  "what shipped in the API", which is the question you actually have.

---

## Types

| Type | Use for |
|---|---|
| `feat` | A new capability for users of SESKit |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `refactor` | Restructuring that changes neither behaviour nor interface |
| `perf` | A change that improves performance |
| `test` | Adding or correcting tests |
| `build` | Build system, Dockerfile, packaging, dependencies |
| `ci` | CI configuration and workflows |
| `chore` | Housekeeping that fits nothing above |
| `revert` | Reverts a previous commit |
| `style` | Formatting only, no code change |

Add `!` before the colon for a breaking change: `feat(api)!: ...`.

## Scopes

Scopes mirror the repository layout, so the vocabulary is already familiar.

| Scope | Covers |
|---|---|
| `api` | `apps/api` — HTTP routes, middleware, app wiring |
| `ui` | Dashboard templates, CSS, design system |
| `worker` | `apps/worker` — background jobs |
| `core` | `packages/core` — config, logging, persistence |
| `provider-ses` | `packages/provider-aws-ses` |
| `sdk` | `packages/sdk-python` |
| `migrations` | Alembic migrations |
| `docker` | Dockerfile, docker-compose |
| `ci` | GitHub Actions |
| `deps` | Dependency bumps |
| `docs` | Files under `docs/`, README, the MVP spec |
| `release` | Version bumps, changelogs, tags |

The scope is **optional**. Omit it for repo-wide changes: `chore: tidy the
repository layout`.

Adding a scope: edit `SCOPES` in `scripts/check_commit_msg.py`. It is the one
place the list lives — the error message renders from the same dictionary, so
it stays accurate automatically.

---

## What gets checked

| Rule | Rejected | Accepted |
|---|---|---|
| Header matches `type(scope): subject` | `added idempotency` | `feat(api): add idempotency` |
| Type is known | `feature(api): ...` | `feat(api): ...` |
| Scope is known (if given) | `feat(backend): ...` | `feat(api): ...` |
| Subject ≤ 72 chars total | a 90-char line | move detail to the body |
| No trailing period | `add support.` | `add support` |
| Not sentence case | `Add support` | `add support` |
| Imperative mood | `added`, `fixes`, `updating` | `add`, `fix`, `update` |
| Blank line before body | subject then body directly | subject, blank line, body |
| Body lines ≤ 100 chars | a long prose line | wrapped prose |

Three deliberate exceptions:

- **Acronyms may start a subject.** `fix(api): SES quota check returns a stale
  value` passes. Only sentence case (`Add`, `Fix`) is caught — the check looks
  for a capital followed by a lower-case letter.
- **Unbreakable tokens are exempt from the body limit.** A long URL or file path
  cannot be wrapped, so a body line with no spaces is never flagged.
- **Imperative verbs ending in `-ed` or `-ing` are allowed.** `add`, `embed`,
  `feed`, `seed`, `extend`, `spread`, `read`, `bring`, `ping`, `string` and
  friends are listed in `IMPERATIVE_EXCEPTIONS`. Add to that set if a legitimate
  verb is ever blocked.

### How the mood check works

It runs narrowest-first: an exact correction where one is known (`added` →
`add`), then `-ed` and `-ing` suffix rules as the general net.

The suffix rules exist because a fixed word list is never finished. The original
version used a list alone, and `rejected` walked straight through it during a
demo — nobody thinks to add that word until it slips past. The exceptions set
above is what keeps the broader rule from producing false positives, which in a
blocking hook are worse than a miss: a missed message is untidy, a wrongly
blocked one stops real work.

### Messages that pass straight through

Git and interactive rebase generate these, and blocking them breaks the tooling:

```text
Merge ...
Revert "..."
fixup! ...
squash! ...
amend! ...
```

An empty message is also ignored — git already aborts the commit on its own, and
a second error would just be noise.

---

## How the hooks work

### The problem

Git runs **exactly one hook per event**. Setting `core.hooksPath` for a repo
replaces the hook directory wholesale — so any hook a contributor has configured
globally stops running in that repo, with no warning.

This is not hypothetical. It was found during setup: a global `core.hooksPath`
was already configured on the development machine, pointing at a `commit-msg`
hook that strips AI co-author trailers. A naive project hook would have silently
disabled it.

It also explains why `pre-commit install` is not used here — pre-commit refuses
to install at all while `core.hooksPath` is set, which is why the Phase 1 ruff
and mypy hooks were configured but never actually running.

### The solution

Both hooks in `.githooks/` **chain to the global hook of the same name first**,
then run the SESKit check:

```text
git commit
    |
    v
.githooks/commit-msg
    |
    ├─→ $(git config --global core.hooksPath)/commit-msg   ← your hook, if any
    |       (may rewrite the message)
    |
    └─→ scripts/check_commit_msg.py                        ← validates the result
            |
            ├─ exit 0  → commit proceeds
            └─ exit 1  → commit blocked, message kept in .git/COMMIT_EDITMSG
```

The order matters: the global hook may rewrite the message, so validation runs
on what is actually about to be committed, not on what was originally typed.

The lookup is generic — it reads whatever `core.hooksPath` the contributor has
set globally. No path is hard-coded, and a contributor with no global hooks
simply skips that step.

### Graceful degradation

Neither hook is allowed to become a wall:

- No Python on `PATH` → the message check is skipped with a notice.
- No `uv` or `pre-commit` → the file checks are skipped with a notice.

Someone fixing a typo through the GitHub web UI should not be blocked by a
missing local dependency. CI runs the same checks, so nothing escapes review.

### CI backstop

A hook only protects contributors who ran the setup. The `commits` job in
`.github/workflows/ci.yml` re-validates every commit in a pull request, so a
contributor who never installed the hooks is caught before review rather than
after merge.

---

## When a hook blocks you

The failure output names every problem at once, lists the valid values, and
shows a correct example. A message with four problems reports all four — you do
not fix one, retry, and discover the next.

**Your message is not lost.** It stays in `.git/COMMIT_EDITMSG`:

```bash
git commit -eF .git/COMMIT_EDITMSG    # reopen it in your editor, fixed and ready
```

### Skipping in an emergency

```bash
git commit --no-verify
```

This bypasses both hooks. CI still runs the same checks, so use it to get
unblocked locally, not to land a non-conforming message.

### Common failures

| Message | Cause | Fix |
|---|---|---|
| `does not match "<type>(<scope>): <subject>"` | No type prefix, or a missing space after the colon | `feat(api): add x` |
| `not a valid scope` | Scope is not in the list | Use a listed scope, omit it, or add yours to `SCOPES` |
| `subject line is N characters` | Over 72 | Move the detail into the body |
| `should not start with a capital` | Sentence case | Lower-case it (acronyms are fine) |
| `use the imperative mood` | Past tense or gerund | `add`, not `added` / `adding` |
| `leave a blank line` | Body starts on line 2 | Insert a blank line after the subject |

---

## Files

| Path | Purpose |
|---|---|
| `.gitmessage` | The template shown in your editor |
| `.githooks/commit-msg` | Chains global, then validates |
| `.githooks/pre-commit` | Chains global, then runs the file checks |
| `scripts/check_commit_msg.py` | The validator — stdlib only, no dependencies |
| `tests/test_commit_msg.py` | 56 tests covering accept, reject, and bypass |
| `.pre-commit-config.yaml` | The file checks, shared with CI |
| `.github/workflows/ci.yml` | The `commits` job — CI backstop |

The validator has tests because it **blocks commits**. A false positive there
stops real work, so both directions are pinned: what must pass, and what must
fail. Change a rule, and the tests tell you what else you changed.
