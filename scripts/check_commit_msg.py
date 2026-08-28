#!/usr/bin/env python3
"""Enforce the SESKit commit message convention.

Invoked by ``.githooks/commit-msg``. Stdlib only, so it costs nothing to run on
every commit and works in any environment.

Format (Conventional Commits):

    <type>(<scope>): <subject>

    <body>

The rejected message is preserved in ``.git/COMMIT_EDITMSG``, so a failed
commit is never lost work - fix it and commit again.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_SUBJECT_LINE = 72
MAX_BODY_LINE = 100

TYPES: dict[str, str] = {
    "feat": "a new capability for users of SESKit",
    "fix": "a bug fix",
    "docs": "documentation only",
    "refactor": "restructuring that changes neither behaviour nor interface",
    "perf": "a change that improves performance",
    "test": "adding or correcting tests",
    "build": "build system, Dockerfile, packaging, dependencies",
    "ci": "CI configuration and workflows",
    "chore": "housekeeping that fits nothing above",
    "revert": "reverts a previous commit",
    "style": "formatting only, no code change",
}

SCOPES: dict[str, str] = {
    "api": "apps/api - HTTP routes, middleware, app wiring",
    "ui": "dashboard templates, CSS, design system",
    "worker": "apps/worker - background jobs",
    "core": "packages/core - config, logging, persistence",
    "provider-ses": "packages/provider-aws-ses",
    "provider-smtp": "packages/provider-smtp",
    "sdk": "packages/sdk-python",
    "migrations": "Alembic migrations",
    "docker": "Dockerfile, docker-compose",
    "ci": "GitHub Actions",
    "deps": "dependency bumps",
    "docs": "files under docs/, README, the MVP spec",
    "release": "version bumps, changelogs, tags",
}

HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[a-z0-9-]+)\))?"
    r"(?P<breaking>!)?"
    r": "
    r"(?P<subject>.+)$"
)

#: Corrections for the forms that come up most often. Used when it can suggest
#: the exact imperative; the suffix rules below are the general net.
NON_IMPERATIVE = {
    "added": "add",
    "adds": "add",
    "adding": "add",
    "fixed": "fix",
    "fixes": "fix",
    "fixing": "fix",
    "updated": "update",
    "updates": "update",
    "updating": "update",
    "removed": "remove",
    "removes": "remove",
    "removing": "remove",
    "changed": "change",
    "changes": "change",
    "changing": "change",
    "created": "create",
    "creates": "create",
    "creating": "create",
    "implemented": "implement",
    "implements": "implement",
    "implementing": "implement",
    "refactored": "refactor",
    "refactors": "refactor",
    "refactoring": "refactor",
    "moved": "move",
    "moves": "move",
    "moving": "move",
    "renamed": "rename",
    "renames": "rename",
    "renaming": "rename",
    "bumped": "bump",
    "bumps": "bump",
    "bumping": "bump",
}

#: Real imperative verbs that happen to end in "ed" or "ing". Without these the
#: suffix rules below would reject perfectly good subjects - and a false
#: positive in a blocking hook stops real work, which is worse than a miss.
IMPERATIVE_EXCEPTIONS = frozenset(
    {
        # -ed
        "add",
        "embed",
        "feed",
        "need",
        "seed",
        "speed",
        "exceed",
        "proceed",
        "succeed",
        "spread",
        "read",
        "shed",
        "shred",
        "breed",
        "bleed",
        "extend",
        # -ing
        "bring",
        "ping",
        "sing",
        "ring",
        "string",
        "spring",
        "cling",
        "fling",
        "swing",
        "sting",
        "wring",
        "config",
    }
)


def imperative_problem(word: str) -> str | None:
    """Return a correction hint if ``word`` is not in the imperative mood.

    Three passes, narrowest first: an exact correction where one is known, then
    the "-ed" and "-ing" suffixes as a general net. A fixed word list alone
    misses too much - "rejected" is not a word anyone thinks to add until it
    slips through.
    """
    if word in IMPERATIVE_EXCEPTIONS:
        return None

    if word in NON_IMPERATIVE:
        return f"{NON_IMPERATIVE[word]!r}, not {word!r}"

    if word.endswith("ing") and len(word) > 5:
        return f"the plain form of {word!r} (for example 'add', not 'adding')"

    if word.endswith("ed") and len(word) > 4:
        return f"the plain form of {word!r} (for example 'add', not 'added')"

    return None


# Messages git generates or that tooling depends on passing through untouched.
BYPASS_PREFIXES = (
    "Merge ",
    "Revert ",
    "fixup!",
    "squash!",
    "amend!",
)


def strip_comments(raw: str) -> str:
    """Drop comment lines and anything after git's verbose-diff scissors."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("# ------------------------ >8 ------------------------"):
            break
        if line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines).strip("\n")


def check(message: str) -> list[str]:
    """Return a list of problems. Empty means the message is acceptable."""
    if not message.strip():
        # git aborts an empty commit message on its own; nothing to add.
        return []

    lines = message.splitlines()
    header = lines[0]

    if header.startswith(BYPASS_PREFIXES):
        return []

    problems: list[str] = []
    match = HEADER_RE.match(header)

    if match is None:
        problems.append(
            f'the subject line does not match "<type>(<scope>): <subject>".\n    got: {header!r}'
        )
        # Everything below reads captured groups, so stop here.
        return problems

    commit_type = match.group("type")
    scope = match.group("scope")
    subject = match.group("subject")

    if commit_type not in TYPES:
        problems.append(
            f"{commit_type!r} is not a valid type. Valid types:\n"
            + "\n".join(f"    {name:<9} {desc}" for name, desc in TYPES.items())
        )

    if scope is not None and scope not in SCOPES:
        problems.append(
            f"{scope!r} is not a valid scope. Valid scopes:\n"
            + "\n".join(f"    {name:<13} {desc}" for name, desc in SCOPES.items())
            + "\n    (the scope is optional - omit it for repo-wide changes)"
        )

    if len(header) > MAX_SUBJECT_LINE:
        problems.append(
            f"the subject line is {len(header)} characters; keep it to {MAX_SUBJECT_LINE}.\n"
            f"    Move the detail into the body - that is what it is for."
        )

    if subject.endswith("."):
        problems.append("the subject line must not end with a period.")

    # Sentence-case start, e.g. "Add support". An all-caps opener is allowed so
    # "SES", "API", and "DKIM" can begin a subject.
    if len(subject) > 1 and subject[0].isupper() and not subject[1].isupper():
        problems.append(
            f"the subject should not start with a capital: {subject.split()[0]!r}.\n"
            f"    Acronyms are fine (SES, API, DKIM) - sentence case is not."
        )

    first_word = subject.split(" ", 1)[0].lower().strip(":,")
    hint = imperative_problem(first_word)
    if hint is not None:
        problems.append(
            f"use the imperative mood: {hint}.\n"
            f'    A subject should complete "This commit will ...".'
        )

    if len(lines) > 1 and lines[1].strip():
        problems.append("leave a blank line between the subject and the body.")

    for number, line in enumerate(lines[2:], start=3):
        # Long unbroken tokens (URLs, paths) cannot be wrapped, so let them be.
        if len(line) > MAX_BODY_LINE and " " in line.strip():
            problems.append(
                f"body line {number} is {len(line)} characters; wrap at {MAX_BODY_LINE}."
            )

    return problems


def render_failure(problems: list[str]) -> str:
    """Render the failure report.

    Deliberately ASCII-only: this goes to a terminal, and a Windows console
    using a legacy code page turns characters like the section sign into
    replacement glyphs, which makes the guidance look broken.
    """
    bullets = "\n".join(f"  - {problem}" for problem in problems)
    return f"""
SESKit commit message check failed
==================================

{bullets}

Expected format
---------------
  <type>(<scope>): <subject>

  Why:  ...
  What: ...

Example
-------
  feat(api): add idempotency key support

  Why:  Duplicate POST /v1/emails retries were sending twice.
  What: Store Idempotency-Key per project; return the existing
        Email record instead of re-sending.

  Refs: SESKit_MVP.md section 12

Your message was kept in .git/COMMIT_EDITMSG - fix it and commit again.
Full reference: .gitmessage
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_commit_msg.py <path-to-commit-message-file>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read commit message file {path}: {exc}", file=sys.stderr)
        return 2

    problems = check(strip_comments(raw))
    if problems:
        print(render_failure(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
