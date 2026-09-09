"""The stylesheet holds to the system it documents (`docs/design/system.md`).

`app.css` opens by naming its own rules - tokens first, components composed
from them, nothing hardcoded - and then drifted from them, quietly, one
declaration at a time. A completed checklist step asked for `--fg-muted`, which
is not a token in this system; the fallback made it look deliberate and it had
simply never been muted. The active item in the segmented control carried a
light-mode shadow written out in `rgb()`, invisible on a dark surface. Two
components set a font size in rem that a token already held exactly.

None of that fails. None of it is visible in a diff either, which is why it is
asserted here: every one is a line that looks fine on its own and is wrong only
against a rule kept somewhere else.

The scope is deliberately narrow. Not every number in a stylesheet should come
from a token - a 1px border and a 16px icon are dimensions, not decisions. What
this file guards is the three kinds of value the system does own: colour, type
size, and the name of a token that has to exist.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLESHEET = ROOT / "apps" / "api" / "src" / "seskit_api" / "static" / "css" / "app.css"

#: Where the token blocks end and the components begin. Everything above is
#: definitions - the one place a literal colour belongs.
_COMPONENTS_START = "2. Reset and base */"

_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
_FONT_SIZE = re.compile(r"font-size:\s*([^;]+);")

#: A size relative to whatever it lands in. `rem` is not one of these - it is
#: relative to the root, which makes it a fixed size wearing a relative unit,
#: and "0.8125rem".endswith("em") is exactly how this guard first passed while
#: two hardcoded sizes sat in the file.
_RELATIVE = re.compile(r"[\d.]+e[m]|%|inherit")
_DEFINED = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)
_REFERENCED = re.compile(r"var\(\s*(--[a-z0-9-]+)")


def _css() -> str:
    return STYLESHEET.read_text(encoding="utf-8")


def _components() -> str:
    """Everything below the token blocks."""
    css = _css()
    assert _COMPONENTS_START in css, "the section markers have moved"
    return css.split(_COMPONENTS_START, 1)[1]


def _rule(css: str, selector: str) -> str:
    """The declarations of one rule, by its exact selector.

    Anchored to the start of a line, because a substring search finds a
    descendant rule first: `.field__control .input {` contains `.input {`, sits
    above `.input` in the file, and made this return the wrong block entirely.
    CI caught that; the search had been fine only because no rule had yet
    described a component inside another one.
    """
    match = re.search(rf"^{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.MULTILINE)
    assert match, f"no rule for {selector}"
    return match.group(1)


def _theme_blocks(css: str) -> list[str]:
    """The three places a token is given a value: the light default, the
    prefers-color-scheme override, and the explicit dark choice.
    """
    blocks = [css[m.start() : css.index("}", m.start())] for m in re.finditer(r":root", css)]
    assert len(blocks) == 3, f"expected three theme blocks, found {len(blocks)}"
    return blocks


def _token(block: str, name: str) -> str | None:
    match = re.search(rf"{re.escape(name)}:\s*([^;]+);", block)
    return match.group(1).strip() if match else None


def _declarations(text: str) -> list[str]:
    """Lines that set something, ignoring comments.

    Comments carry hex values on purpose - the tokens explain their own
    contrast ratios against named colours - and a guard that read those would
    be a guard that punished the file for being documented.
    """
    without_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return [line.strip() for line in without_comments.splitlines() if ":" in line]


# ----------------------------------------------------------------- colour ---


def test_no_component_names_a_colour() -> None:
    """Colour is a token, and the theme blocks are the only place one is
    written out.

    A literal below them is a value that exists in exactly one theme. The
    segmented control's active item carried `rgb(16 24 40 / 6%)`, which is a
    light-mode shadow - on a dark surface it is a shadow nobody can see.
    """
    offenders = [line for line in _declarations(_components()) if _COLOUR.search(line)]

    assert not offenders, f"colour written out below the token blocks: {offenders}"


# ------------------------------------------------------------------- type ---


def test_no_component_invents_a_type_size() -> None:
    """The scale is eight tokens. A rem value below them is either one of the
    eight written out - `.segmented__item` had `0.8125rem`, which is
    `--text-sm` exactly - or a ninth size nobody decided on.

    `em` is allowed: it is relative to whatever it lands in, which is the point
    of the one use of it here - inline `code` sized against its surrounding
    text rather than against the page.
    """
    offenders = [
        value.strip()
        for line in _declarations(_components())
        for value in _FONT_SIZE.findall(line)
        if "var(--" not in value and not _RELATIVE.fullmatch(value.strip())
    ]

    assert not offenders, f"font sizes that are not on the scale: {offenders}"


# ------------------------------------------------------------------ names ---


def test_every_token_used_is_a_token_that_exists() -> None:
    """The one that had already happened.

    `.setup__step--done` asked for `var(--fg-muted, inherit)`. There is no
    `--fg-muted` in this system - it is `--text-muted` - so a completed step
    fell through to the fallback and was never muted at all. The fallback is
    what made it invisible: with one, a misspelt token renders as something
    plausible instead of as nothing.
    """
    css = _css()
    defined = set(_DEFINED.findall(css))
    used = set(_REFERENCED.findall(css))

    assert used <= defined, f"used but never defined: {sorted(used - defined)}"


# --------------------------------------------------------------- surfaces ---


def test_a_code_block_is_a_different_surface_from_the_card_under_it() -> None:
    """It used `--surface-raised`, which in the light theme is `#ffffff` - the
    same white as the card it sits inside. So a code block had nothing but its
    border, in the theme most people read the docs in.

    Raised was the wrong direction as well as the wrong colour. A code block is
    a well in a card, not something floating over it; `--surface-raised` still
    means what it says for the things that do float - the toast and the skip
    link, both of which sit over the page rather than in a card.
    """
    css = _css()

    assert "background: var(--surface-sunken);" in _rule(css, ".code")

    for block in _theme_blocks(css):
        surface = _token(block, "--surface")
        sunken = _token(block, "--surface-sunken")
        assert surface and sunken, block[:40]
        assert surface != sunken, f"a sunken surface equal to the card: {surface}"


# ------------------------------------------------------------------ guard ---


def test_the_guards_would_notice() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    assert _COLOUR.search("box-shadow: 0 1px 2px rgb(16 24 40 / 6%);")
    assert _COLOUR.search("color: #14181f;")
    assert not _COLOUR.search("color: var(--text);")
    # A shadow built from a token is fine - it is the literal that is not.
    assert not _COLOUR.search("box-shadow: 0 0 0 3px var(--accent-subtle);")

    assert _FONT_SIZE.findall("font-size: 0.8125rem;") == ["0.8125rem"]
    # The one that caught this guard out: rem ends in "em" and is not relative
    # to anything the component sits in.
    assert not _RELATIVE.fullmatch("0.8125rem")
    assert _RELATIVE.fullmatch("0.9em")
    assert _REFERENCED.findall("color: var(--fg-muted, inherit);") == ["--fg-muted"]
    assert _DEFINED.findall("  --text-sm: 0.8125rem;") == ["--text-sm"]

    # `_rule` takes the rule whose selector *is* the one asked for, not the
    # first one containing it as a substring. A descendant rule reads as a
    # match and can sit above its own component in the file, which is how a
    # height assertion ended up reading `.field__control .input`.
    nested = """.field__control .input {
  flex: 1 1 auto;
}

.input {
  height: 34px;
}
"""
    assert "height" in _rule(nested, ".input")
    assert "flex" in _rule(nested, ".field__control .input")

    # Comments are not declarations: the token block explains its contrast
    # ratios against hex values, and reading those would fail the file for
    # documenting itself.
    assert _declarations("/* fails against #14171d */\ncolor: var(--text);") == [
        "color: var(--text);"
    ]
