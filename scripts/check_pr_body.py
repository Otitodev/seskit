#!/usr/bin/env python3
"""Enforce the SESKit pull request description.

Invoked by ``.github/workflows/pull-request.yml``. Stdlib only, for the same
reason ``check_commit_msg.py`` is: it should run anywhere, instantly, with
nothing installed.

**Why CI rather than a git hook.** A pull request body is a GitHub object that
does not exist until after the push, so no client-side hook can see it -
unlike a commit message, which is why that one *is* a hook. This is the only
place the rule can live.

**And why the template alone is not enough.** ``.github/pull_request_template.md``
pre-fills the web form and nothing else. ``gh pr create --body "..."`` never
sees it, which is how most of this repository's pull requests are opened. So
the template is the prompt and this is the rule.

Three rules, chosen because each is mechanically checkable and a human can
tell at a glance whether it was followed:

1. The headings are there.
2. "What and why" says something. A heading with nothing under it is a heading.
3. No checkbox is left unticked. Tick it or delete the line - an untouched box
   means nobody decided, and recording an undecided thing as if it were a
   result is worse than not listing it.

What it deliberately does not do is judge the prose. It can prove a section
exists and is not empty; it cannot prove the content is not "n/a", and
pretending otherwise would make it a ritual rather than a check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Every heading the template ships with. Matched case-insensitively at any
#: level, so promoting `##` to `#` does not fail the check.
REQUIRED_HEADINGS: tuple[str, ...] = ("What and why", "Checks")

#: The section that has to carry prose, and how much. Low on purpose: this is
#: a floor against an empty heading, not an essay requirement.
PROSE_SECTION = "What and why"
MIN_PROSE = 40

#: `<!-- ... -->`, including the multi-line comments the template uses for its
#: instructions. Stripped before measuring, or the template's own guidance
#: would count as the author's writing.
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)

#: A list item whose box is empty. `- [ ]`, `* [ ]`, and the `[]` people type
#: when they forget the space.
UNTICKED_RE = re.compile(r"^\s*[-*+]\s*\[\s*\]\s*(?P<label>.*)$", re.MULTILINE)


def _strip_comments(body: str) -> str:
    return COMMENT_RE.sub("", body)


def _headings(body: str) -> list[str]:
    return [match.group("title").strip() for match in HEADING_RE.finditer(body)]


def _section(body: str, title: str) -> str:
    """The text under ``title``, up to the next heading of any level.

    Returns an empty string when the heading is absent, which the caller has
    already reported as a separate failure - so this never has to distinguish
    "missing" from "empty".
    """
    wanted = title.casefold()
    matches = list(HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        if match.group("title").strip().casefold() != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.end() : end].strip()
    return ""


def check(body: str) -> list[str]:
    """Every problem with this description. Empty means it passes.

    All of them at once rather than the first: an author who fixes one thing,
    pushes, and is told about the next has been made to wait through CI twice
    for something one message could have said.
    """
    problems: list[str] = []
    text = _strip_comments(body)

    present = {heading.casefold() for heading in _headings(text)}
    for heading in REQUIRED_HEADINGS:
        if heading.casefold() not in present:
            problems.append(f"missing the '## {heading}' section")

    if PROSE_SECTION.casefold() in present:
        prose = _section(text, PROSE_SECTION)
        if len(prose) < MIN_PROSE:
            problems.append(
                f"'## {PROSE_SECTION}' needs at least {MIN_PROSE} characters "
                f"saying what changed and why - it has {len(prose)}"
            )

    for match in UNTICKED_RE.finditer(text):
        label = match.group("label").strip() or "(no label)"
        problems.append(f"unticked box left in place: {label}")

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_pr_body.py <file>", file=sys.stderr)
        return 2

    body = Path(argv[1]).read_text(encoding="utf-8")
    if not body.strip():
        print("The pull request description is empty.", file=sys.stderr)
        print("", file=sys.stderr)
        print(_help(), file=sys.stderr)
        return 1

    problems = check(body)
    if not problems:
        return 0

    print("This pull request description does not follow the template:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("", file=sys.stderr)
    print(_help(), file=sys.stderr)
    return 1


def _help() -> str:
    return (
        "Edit the description on the pull request; the check re-runs when you save.\n"
        "The template is .github/pull_request_template.md - copy it if you opened\n"
        "this with `gh pr create --body`, which never sees it."
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
