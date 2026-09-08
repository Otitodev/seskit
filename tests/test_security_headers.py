"""Security response headers (§22, §31 Phase 13).

Two things are being protected here and they pull in opposite directions.

**The policy must be strict enough to matter.** A CSP carrying
`'unsafe-inline'` is a header that passes a scanner and stops nothing, and it
is what a codebase ends up with when an inline script is added without one.

**And the pages must still work.** The theme script has to run before first
paint or a dark-theme user sees a white flash, and a nonce that is missing does
not raise — the script is silently refused and the page looks subtly wrong to
somebody who is not looking at a console.

So there is a guard over the template tree as well as tests over the headers:
an inline `<script>` without a nonce is a bug that ships quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import AsyncClient
from seskit_api.middleware.security import BASE_POLICY, HSTS, policy_for

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "apps" / "api" / "src" / "seskit_api" / "templates"

#: An opening <script> tag with no `src`, i.e. one whose body is inline.
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>")

#: An inline event handler attribute - onchange=, onclick=, and the rest.
#:
#: Worth its own guard rather than being folded into the one above, because the
#: fix is different. An inline script can be made to run by giving it the
#: nonce; an inline handler cannot be made to run at all under this policy, so
#: the only fix is to move it into a file.
_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=\s*[\"']")


# ---------------------------------------------------------------- headers ---


async def test_every_response_carries_the_policy(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert "Content-Security-Policy" in response.headers


async def test_the_policy_does_not_allow_inline_scripts(client: AsyncClient) -> None:
    """The whole point. `'unsafe-inline'` is what a policy degrades to when an
    inline script is added without a nonce, and it stops nothing.
    """
    policy = (await client.get("/healthz")).headers["Content-Security-Policy"]

    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


async def test_the_policy_allows_nothing_from_anywhere_else(client: AsyncClient) -> None:
    """§5 forbids a build step, so every script and stylesheet is a local file.
    A policy that admitted a CDN would be permitting something nothing uses.
    """
    policy = (await client.get("/healthz")).headers["Content-Security-Policy"]

    assert policy.startswith("default-src 'self'")
    assert "https://" not in policy


async def test_the_page_cannot_be_framed(client: AsyncClient) -> None:
    """Every destructive action on the dashboard is a one-click form, which is
    exactly what clickjacking needs.
    """
    headers = (await client.get("/healthz")).headers

    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


async def test_a_form_cannot_be_retargeted(client: AsyncClient) -> None:
    """`form-action` is the one directive `X-Frame-Options` has no answer for:
    injected markup that posts a CSRF token to somebody else's origin.
    """
    policy = (await client.get("/healthz")).headers["Content-Security-Policy"]

    assert "form-action 'self'" in policy


async def test_a_response_is_not_sniffed_as_something_else(client: AsyncClient) -> None:
    headers = (await client.get("/healthz")).headers

    assert headers["X-Content-Type-Options"] == "nosniff"


async def test_a_token_in_a_url_does_not_leave_with_the_referer(
    client: AsyncClient,
) -> None:
    """An unsubscribe link and an email id both live in a path. `same-origin`
    keeps SESKit's own navigation while stopping either reaching a third party.
    """
    headers = (await client.get("/healthz")).headers

    assert headers["Referrer-Policy"] == "same-origin"


async def test_local_development_is_not_pinned_to_https(client: AsyncClient) -> None:
    """HSTS on localhost is not caution, it is damage: a browser that has seen
    it refuses every other project served from http://localhost, and the only
    cure is clearing HSTS state by hand.
    """
    response = await client.get("/healthz")

    assert "Strict-Transport-Security" not in response.headers


def test_the_policy_is_pinned_to_https_outside_local() -> None:
    """The other half of the rule above. Asserted on the value rather than
    through a client, because a settings object with ENVIRONMENT set is a
    different application.
    """
    assert HSTS.startswith("max-age=")
    assert "includeSubDomains" in HSTS


# ------------------------------------------------------------------ nonce ---


async def test_each_response_gets_its_own_nonce(client: AsyncClient) -> None:
    """A nonce reused across responses is a nonce an attacker can read from one
    page and use in an injection on the next.
    """
    first = (await client.get("/healthz")).headers["Content-Security-Policy"]
    second = (await client.get("/healthz")).headers["Content-Security-Policy"]

    assert first != second


async def test_the_rendered_page_carries_the_nonce_the_header_names(
    signed_in_client: AsyncClient,
) -> None:
    """The test that would catch the whole thing being wired up wrong. A
    mismatch does not raise anywhere - the script is simply refused, and the
    page looks subtly wrong to somebody who is not watching a console.
    """
    page = await signed_in_client.get("/emails")

    nonce = re.search(r"'nonce-([^']+)'", page.headers["Content-Security-Policy"])
    assert nonce is not None
    assert f'nonce="{nonce.group(1)}"' in page.text


def test_the_policy_names_a_nonce() -> None:
    assert "'nonce-abc'" in policy_for("abc")
    assert "script-src 'self'" in policy_for("abc")


@pytest.mark.parametrize(
    "template",
    sorted(path.relative_to(TEMPLATES).as_posix() for path in TEMPLATES.rglob("*.html")),
)
def test_no_template_ships_an_inline_script_without_a_nonce(template: str) -> None:
    """The guard, over the template tree rather than over one page.

    The next inline script somebody adds will be added to a template, not to a
    test, and without this it would ship silently unrunnable.
    """
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    unnonced = [tag for tag in _INLINE_SCRIPT.findall(text) if "csp_nonce" not in tag]

    assert not unnonced, f"{template} has an inline script with no nonce: {unnonced}"


@pytest.mark.parametrize(
    "template",
    sorted(path.relative_to(TEMPLATES).as_posix() for path in TEMPLATES.rglob("*.html")),
)
def test_no_template_ships_an_inline_event_handler(template: str) -> None:
    """A nonce does not license one, so this is not a lesser version of the
    guard above - it is the case that has no fix short of moving the code.

    The project switcher shipped `onchange="this.form.submit()"` for two
    phases. It was refused on every page load, in silence, and the dropdown
    simply did nothing: no error a user would see, and a <noscript> fallback
    that stayed hidden because scripting was enabled the whole time.
    """
    text = (TEMPLATES / template).read_text(encoding="utf-8")

    assert not _INLINE_HANDLER.findall(text), (
        f"{template} has an inline event handler, which the CSP refuses. "
        f"Move it into static/js/app.js."
    )


def test_the_guard_would_notice() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    assert _INLINE_SCRIPT.findall("<script>alert(1)</script>") == ["<script>"]
    assert _INLINE_SCRIPT.findall('<script src="/js/app.js"></script>') == []
    assert _INLINE_SCRIPT.findall('<script nonce="{{ csp_nonce }}">') == [
        '<script nonce="{{ csp_nonce }}">'
    ]

    assert _INLINE_HANDLER.findall('<select onchange="this.form.submit()">')
    assert _INLINE_HANDLER.findall("<button onclick='go()'>")
    # Not every attribute beginning with "on": the data attribute that replaced
    # the handler must not read as one, or the guard fails on its own fix.
    assert _INLINE_HANDLER.findall("<select data-auto-submit>") == []
    assert _INLINE_HANDLER.findall('<a href="/only">') == []


# ----------------------------------------------------------------- policy ---


def test_the_base_policy_leaves_scripts_to_be_completed() -> None:
    """`script-src` is deliberately absent from the constant: a policy with a
    script directive that forgot the nonce would be worse than no constant at
    all, because it would look complete.
    """
    assert "script-src" not in BASE_POLICY
