"""The commit message hook.

The hook blocks commits, so a false positive is expensive - it stops real work.
These tests pin both directions: what must pass, and what must fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is a repo tool directory, not an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_commit_msg import check, main, strip_comments


def _problems(message: str) -> list[str]:
    return check(strip_comments(message))


# --------------------------------------------------------------- acceptance --


@pytest.mark.parametrize(
    "message",
    [
        "feat(api): add idempotency key support",
        "fix(worker): retry webhook delivery on connection reset",
        "docs: explain the SES sandbox in the quickstart",
        "chore: reserve the seskit namespace",
        "feat(api)!: drop the v0 send endpoint",
        "refactor(core): extract the provider protocol",
        "ci: run mypy before the test suite",
        "fix(provider-ses): normalise MessageRejected into email_rejected",
        "feat(ui): add the domain verification wizard",
        "build(docker): pin the postgres image to 16-alpine",
    ],
)
def test_valid_messages_are_accepted(message: str) -> None:
    assert _problems(message) == []


def test_acronyms_may_start_the_subject() -> None:
    """SES, API, and DKIM are legitimate openers; sentence case is not."""
    assert _problems("fix(api): SES quota check returns a stale value") == []


def test_body_is_accepted_after_a_blank_line() -> None:
    message = (
        "feat(api): add idempotency key support\n"
        "\n"
        "Why:  Duplicate POST /v1/emails retries were sending twice.\n"
        "What: Store Idempotency-Key per project.\n"
        "\n"
        "Refs: SESKit_MVP.md §12"
    )

    assert _problems(message) == []


def test_scope_is_optional() -> None:
    assert _problems("chore: tidy the repository layout") == []


# ------------------------------------------------------------------ rejects --


def test_missing_type_is_rejected() -> None:
    problems = _problems("add idempotency key support")

    assert len(problems) == 1
    assert "does not match" in problems[0]


def test_unknown_type_is_rejected_and_lists_valid_types() -> None:
    problems = _problems("feature(api): add idempotency key support")

    assert any("not a valid type" in p for p in problems)
    # The error has to teach, not just reject.
    assert any("feat" in p and "fix" in p for p in problems)


def test_unknown_scope_is_rejected_and_lists_valid_scopes() -> None:
    problems = _problems("feat(backend): add idempotency key support")

    assert any("not a valid scope" in p for p in problems)
    assert any("provider-ses" in p for p in problems)


def test_scope_error_mentions_that_scope_is_optional() -> None:
    problems = _problems("feat(nonsense): do a thing")

    assert any("optional" in p for p in problems)


def test_overlong_subject_is_rejected() -> None:
    message = "feat(api): " + "x" * 80

    problems = _problems(message)

    assert any("subject line is" in p and "characters" in p for p in problems)


def test_subject_at_the_limit_is_accepted() -> None:
    """72 characters exactly must pass - an off-by-one here blocks real commits."""
    subject = "a" * (72 - len("feat(api): "))
    message = f"feat(api): {subject}"

    assert len(message) == 72
    assert _problems(message) == []


def test_trailing_period_is_rejected() -> None:
    problems = _problems("feat(api): add idempotency key support.")

    assert any("must not end with a period" in p for p in problems)


def test_sentence_case_subject_is_rejected() -> None:
    problems = _problems("feat(api): Add idempotency key support")

    assert any("should not start with a capital" in p for p in problems)


@pytest.mark.parametrize(
    ("wrong", "right"),
    [("added", "add"), ("adds", "add"), ("fixing", "fix"), ("updates", "update")],
)
def test_known_non_imperative_is_rejected_with_the_exact_correction(wrong: str, right: str) -> None:
    problems = _problems(f"feat(api): {wrong} idempotency key support")

    assert any(f"{right!r}" in p and f"{wrong!r}" in p for p in problems)


@pytest.mark.parametrize(
    "wrong",
    # None of these are in the correction table; the suffix rules must catch
    # them. A fixed word list alone let "rejected" through unnoticed.
    ["rejected", "normalised", "extracted", "validating", "persisting", "deduplicated"],
)
def test_unlisted_non_imperative_is_still_rejected(wrong: str) -> None:
    problems = _problems(f"feat(api): {wrong} the request payload")

    assert any("imperative mood" in p for p in problems)


@pytest.mark.parametrize(
    "verb",
    # Genuine imperatives that end in -ed or -ing. A false positive here blocks
    # a legitimate commit, which is worse than missing one.
    ["add", "embed", "feed", "seed", "extend", "spread", "read", "bring", "ping", "string"],
)
def test_imperative_verbs_ending_in_ed_or_ing_are_accepted(verb: str) -> None:
    assert _problems(f"feat(api): {verb} the request id header") == []


def test_short_words_are_not_flagged_by_the_suffix_rules() -> None:
    """Words like red, bed, and wed are too short to be worth guessing at."""
    assert _problems("feat(ui): red badge for bounced mail") == []


def test_missing_blank_line_before_body_is_rejected() -> None:
    message = "feat(api): add idempotency key support\nWhy: duplicates were sent twice."

    problems = _problems(message)

    assert any("blank line" in p for p in problems)


def test_overlong_body_line_is_rejected() -> None:
    message = "feat(api): add support\n\n" + ("word " * 40)

    problems = _problems(message)

    assert any("body line" in p for p in problems)


def test_long_unbroken_body_token_is_allowed() -> None:
    """A URL or path cannot be wrapped, so it must not be flagged."""
    url = "https://docs.aws.amazon.com/ses/latest/dg/" + "x" * 90
    message = f"docs: link the SES quota reference\n\n{url}"

    assert _problems(message) == []


# ------------------------------------------------------------------ bypass --


@pytest.mark.parametrize(
    "message",
    [
        "Merge branch 'main' into feature/idempotency",
        'Revert "feat(api): add idempotency key support"',
        "fixup! feat(api): add idempotency key support",
        "squash! feat(api): add idempotency key support",
    ],
)
def test_generated_and_rebase_messages_pass_through(message: str) -> None:
    """git and interactive rebase generate these; blocking them breaks tooling."""
    assert _problems(message) == []


def test_empty_message_is_left_to_git() -> None:
    """git already aborts an empty message - a second error would just be noise."""
    assert _problems("") == []
    assert _problems("\n\n") == []


# ----------------------------------------------------------------- comments --


def test_template_comments_are_ignored() -> None:
    """The .gitmessage template is mostly comments; they must not be validated."""
    message = (
        "feat(api): add idempotency key support\n"
        "\n"
        "# <type>(<scope>): <subject>\n"
        "# TYPES\n"
        "#   feat      a new capability\n"
    )

    assert _problems(message) == []


def test_verbose_diff_section_is_ignored() -> None:
    """`git commit --verbose` appends the diff below a scissors line."""
    message = (
        "feat(api): add idempotency key support\n"
        "\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/very/long/path/that/exceeds/the/body/line/limit/by/quite/a/lot.py\n"
    )

    assert _problems(message) == []


# --------------------------------------------------------------------- cli --


def test_main_returns_zero_for_a_valid_message(tmp_path: Path) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("feat(api): add idempotency key support", encoding="utf-8")

    assert main(["check_commit_msg.py", str(path)]) == 0


def test_main_returns_one_for_an_invalid_message(tmp_path: Path) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("Added idempotency support.", encoding="utf-8")

    assert main(["check_commit_msg.py", str(path)]) == 1


def test_main_returns_two_without_an_argument() -> None:
    assert main(["check_commit_msg.py"]) == 2


def test_main_returns_two_for_a_missing_file(tmp_path: Path) -> None:
    assert main(["check_commit_msg.py", str(tmp_path / "nope")]) == 2
