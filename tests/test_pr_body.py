"""The pull request description check.

A CI gate on somebody's description is the kind of rule that turns into a
ritual: annoying enough to resent, easy enough to satisfy without thinking. So
the tests pin both edges deliberately - what it must catch, and, just as
importantly, what it must let through.

The template ships in `.github/pull_request_template.md`, and the last test in
this file asserts that the template itself passes. A template that fails its
own check would be an unusually good way to lose everybody's goodwill.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is a repo tool directory, not an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_pr_body import MIN_PROSE, check, main

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".github" / "pull_request_template.md"

GOOD = """## What and why

The refcount query had no index behind it, so every identity delete scanned
the table. This adds one.

## Checks

- [x] Migrations: `uv run alembic upgrade head` runs
- [x] Docs: not needed
"""


# ------------------------------------------------------------- acceptance ---


def test_a_filled_in_description_passes() -> None:
    assert check(GOOD) == []


def test_a_deleted_checklist_line_is_fine() -> None:
    """The instruction is "tick it or delete it". Deleting has to actually
    work, or the rule reads as "tick everything" and people tick everything.
    """
    body = GOOD.replace("- [x] Docs: not needed\n", "")

    assert check(body) == []


def test_every_checklist_line_may_be_deleted() -> None:
    """A documentation-only change has nothing to say about migrations. An
    empty Checks section is a decision, not an omission.
    """
    body = "## What and why\n\n" + "x" * MIN_PROSE + "\n\n## Checks\n"

    assert check(body) == []


def test_a_promoted_heading_still_counts() -> None:
    """`#` instead of `##`. The rule is about the section being there, not
    about how somebody chose to size it.
    """
    body = GOOD.replace("## ", "# ")

    assert check(body) == []


def test_case_is_not_the_point() -> None:
    body = GOOD.replace("## What and why", "## What And Why")

    assert check(body) == []


def test_extra_sections_are_welcome() -> None:
    """The template is a floor. Somebody adding "## Screenshots" or
    "## Trade-offs" is doing better than the rule asks, and must not be
    punished for it.
    """
    body = GOOD + "\n## Trade-offs\n\nThe index costs writes.\n"

    assert check(body) == []


# ------------------------------------------------------------- refusal ---


def test_a_missing_section_is_named() -> None:
    body = "## Checks\n\n- [x] Docs: not needed\n"

    problems = check(body)

    assert any("What and why" in problem for problem in problems)


def test_an_empty_section_is_not_a_section() -> None:
    """The failure this exists for. A heading with nothing under it satisfies
    a check that only looked for headings, and says nothing to a reviewer.
    """
    body = "## What and why\n\n## Checks\n\n- [x] Docs: not needed\n"

    problems = check(body)

    assert any("at least" in problem for problem in problems)


def test_the_templates_own_instructions_do_not_count_as_prose() -> None:
    """The comments in the template are longer than the minimum. If they
    counted, opening a pull request and changing nothing would pass.
    """
    body = (
        "## What and why\n\n<!-- "
        + "x" * (MIN_PROSE * 3)
        + " -->\n\n## Checks\n\n- [x] Docs: not needed\n"
    )

    problems = check(body)

    assert any("at least" in problem for problem in problems)


def test_an_unticked_box_is_refused() -> None:
    """An untouched box means nobody decided. Recording an undecided thing as
    if it were a result is worse than not listing it at all.
    """
    body = GOOD.replace("- [x] Docs: not needed", "- [ ] Docs: not needed")

    problems = check(body)

    assert any("unticked" in problem for problem in problems)


def test_an_unticked_box_says_which_one() -> None:
    """With three boxes, "an unticked box" sends somebody hunting."""
    body = GOOD.replace("- [x] Docs: not needed", "- [ ] Docs: not needed")

    assert any("Docs: not needed" in problem for problem in check(body))


@pytest.mark.parametrize("box", ["- [ ]", "-  [ ]", "* [ ]", "+ [ ]", "- []"])
def test_the_ways_people_write_an_empty_box(box: str) -> None:
    """`- []` is what you get typing it by hand and forgetting the space.
    GitHub does not render it as a checkbox, so it would otherwise slip past
    as ordinary text.
    """
    body = GOOD.replace("- [x] Docs: not needed", f"{box} Docs: not needed")

    assert any("unticked" in problem for problem in check(body))


def test_every_problem_is_reported_at_once() -> None:
    """An author who fixes one thing, saves, and is then told about the next
    has been made to wait through CI twice for something one message could
    have said.
    """
    body = "## Checks\n\n- [ ] Docs\n- [ ] Migrations\n"

    assert len(check(body)) >= 3


# ---------------------------------------------------------------- running ---


def test_an_empty_description_fails(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_text("   \n\n", encoding="utf-8")

    assert main(["check_pr_body.py", str(path)]) == 1


def test_a_good_description_exits_zero(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_text(GOOD, encoding="utf-8")

    assert main(["check_pr_body.py", str(path)]) == 0


def test_the_failure_says_where_the_template_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Most of this repository's pull requests are opened with
    `gh pr create --body`, which never sees the template. Somebody hitting
    this check may never have read it.
    """
    path = tmp_path / "body.md"
    path.write_text("nothing here\n", encoding="utf-8")

    main(["check_pr_body.py", str(path)])

    assert "pull_request_template.md" in capsys.readouterr().err


# --------------------------------------------------------------- template ---


def test_the_template_exists_where_github_looks_for_it() -> None:
    """GitHub finds it by path. A renamed file is a template that silently
    stops pre-filling anything.
    """
    assert TEMPLATE.is_file()


def test_the_template_names_every_required_section() -> None:
    """The check and the template have to agree. If they drift, the template
    becomes a form that cannot be completed.
    """
    from check_pr_body import REQUIRED_HEADINGS

    text = TEMPLATE.read_text(encoding="utf-8")

    for heading in REQUIRED_HEADINGS:
        assert heading in text


def test_the_blank_template_does_not_pass() -> None:
    """Submitting it untouched is exactly the case this rejects: every box
    unticked and nothing written. If it passed, the check would do nothing.
    """
    assert check(TEMPLATE.read_text(encoding="utf-8")) != []
