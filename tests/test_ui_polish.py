"""Action feedback (§17, `docs/design/system.md`).

Until this, every state-changing form in the dashboard re-rendered its page and
said nothing. A successful save looked exactly like a save that never happened,
which is the failure mode that makes a user press the button again.

The test that matters here is the escaping one. Two confirmations quote input
the user typed - an identity value and an API key name - so the flash is a path
from a form field to rendered HTML, and it needs holding to the same standard
as the email body on the Emails page.
"""

from __future__ import annotations

import re

from httpx import AsyncClient
from seskit_core.models import APIKey, WebhookEndpoint
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _csrf(client: AsyncClient, path: str) -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# ------------------------------------------------------------------ toast ---


async def test_a_page_view_says_nothing(signed_in_client: AsyncClient) -> None:
    """A toast belongs to an action. Arriving on a page is not one, and a
    confirmation with nothing behind it teaches people to ignore the next one.
    """
    page = await signed_in_client.get("/webhooks")

    assert "data-toast" not in page.text


async def test_an_action_confirms_itself(signed_in_client: AsyncClient) -> None:
    response = await signed_in_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(signed_in_client, "/webhooks"),
        },
    )

    assert response.status_code == 200
    assert "data-toast" in response.text
    assert "Endpoint added." in response.text


async def test_a_refused_action_does_not_confirm(signed_in_client: AsyncClient) -> None:
    """The refusal is the message. A toast beside it would be two answers to
    one question, and the reassuring one is the wrong one.
    """
    response = await signed_in_client.post(
        "/webhooks",
        # A scheme refused in every environment. A private address would not
        # do: the test environment is local, where those are allowed on purpose
        # so a developer can point a webhook at their own machine.
        data={
            "url": "ftp://example.com/x",
            "csrf_token": await _csrf(signed_in_client, "/webhooks"),
        },
    )

    assert response.status_code == 400
    assert "data-toast" not in response.text


async def test_the_toast_announces_itself_without_stealing_focus(
    signed_in_client: AsyncClient,
) -> None:
    """`role="status"` is announced politely. `alert` would interrupt, and the
    user is usually still on the control they just pressed.
    """
    response = await signed_in_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(signed_in_client, "/webhooks"),
        },
    )

    assert 'role="status"' in response.text
    assert 'aria-live="polite"' in response.text


async def test_the_message_is_in_the_html_not_built_by_javascript(
    signed_in_client: AsyncClient,
) -> None:
    """It is rendered server-side and floated afterwards, so it survives a page
    that loads with scripts blocked rather than existing only if they arrive.
    """
    response = await signed_in_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(signed_in_client, "/webhooks"),
        },
    )

    # Present in the markup, and not yet floating - app.js adds that attribute.
    assert "Endpoint added." in response.text
    assert "data-toast-floating" not in response.text


# --------------------------------------------------------------- escaping ---


async def test_a_confirmation_quoting_the_user_escapes_it(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The API key name reaches the confirmation, so it reaches the page.

    Same reasoning as the HTML body on the Emails page: the value is chosen by
    someone with dashboard access, and rendering it unescaped would run their
    markup inside an authenticated session.
    """
    payload = '<script>alert("xss")</script>'

    created = await signed_in_client.post(
        "/api-keys",
        data={"name": payload, "csrf_token": await _csrf(signed_in_client, "/api-keys")},
    )
    assert created.status_code == 200

    key_id = await db_session.scalar(select(APIKey.id))
    assert key_id is not None

    revoked = await signed_in_client.post(
        f"/api-keys/{key_id}/revoke",
        data={"csrf_token": await _csrf(signed_in_client, "/api-keys")},
    )

    assert "Revoked" in revoked.text
    assert payload not in revoked.text
    assert "&lt;script&gt;" in revoked.text


# ------------------------------------------------------------- honest text ---


async def test_an_unchanged_url_does_not_claim_to_have_changed(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Resubmitting the same address is the case where a user is checking
    carefully that what they typed is what is stored. Saying "changed" there is
    a small lie in the one place it would be noticed.
    """
    url = "https://hooks.example.com/seskit"

    await signed_in_client.post(
        "/webhooks", data={"url": url, "csrf_token": await _csrf(signed_in_client, "/webhooks")}
    )

    endpoint_id = await db_session.scalar(select(WebhookEndpoint.id))
    assert endpoint_id is not None

    response = await signed_in_client.post(
        f"/webhooks/{endpoint_id}/url",
        data={"url": url, "csrf_token": await _csrf(signed_in_client, "/webhooks")},
    )

    # Not the whole sentence: the apostrophe in "endpoint's" is escaped to
    # &#39; on the way out, which is the autoescaping doing its job.
    assert "already the endpoint" in response.text
    assert "Endpoint URL changed." not in response.text


async def test_pausing_and_resuming_say_which_one_happened(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    await signed_in_client.post(
        "/webhooks",
        data={
            "url": "https://hooks.example.com/seskit",
            "csrf_token": await _csrf(signed_in_client, "/webhooks"),
        },
    )

    endpoint_id = await db_session.scalar(select(WebhookEndpoint.id))
    assert endpoint_id is not None

    paused = await signed_in_client.post(
        f"/webhooks/{endpoint_id}/enabled",
        data={"enabled": "", "csrf_token": await _csrf(signed_in_client, "/webhooks")},
    )
    assert "Endpoint paused." in paused.text

    resumed = await signed_in_client.post(
        f"/webhooks/{endpoint_id}/enabled",
        data={"enabled": "on", "csrf_token": await _csrf(signed_in_client, "/webhooks")},
    )
    assert "Endpoint enabled." in resumed.text


# --------------------------------------------------------- loading states ---


def test_every_htmx_swap_says_it_is_working() -> None:
    """Read against the templates themselves rather than a rendered page.

    The point is not the two swaps that exist now - it is the third one, added
    later by someone who has not read this file. `.spinner` and `.skeleton` sat
    in the stylesheet unused since Phase 1 precisely because nothing failed
    when they were skipped.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/api/src/seskit_api/templates"

    missing: list[str] = []
    for template in sorted(root.rglob("*.html")):
        markup = template.read_text(encoding="utf-8")
        # Each tag that opens a request. hx-post on a form is the same promise
        # as hx-get on a link.
        for tag in re.findall(r"<[^>]*\bhx-(?:get|post)=[^>]*>", markup, re.DOTALL):
            if "hx-indicator" in tag:
                continue
            # A poll is exempt, and only a poll. The rule is about answering a
            # click: nobody asked for the 30-second status refresh in
            # base.html, and a spinner blinking on it twice a minute is noise
            # attached to a request the user did not make. It carries its own
            # "Checking" badge, which is the state that does belong there.
            if re.search(r'hx-trigger="[^"]*\bevery\b', tag):
                continue
            missing.append(f"{template.relative_to(root).as_posix()}: {tag[:70]}...")

    assert not missing, "HTMX requests with no loading state:\n" + "\n".join(missing)


def test_the_indicator_uses_the_styles_that_already_existed() -> None:
    """`.htmx-indicator` fades opacity instead of toggling display, so the row
    does not jump when a spinner appears. Asserted because a future spinner
    added without the class would look correct until someone clicked it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/api/src/seskit_api"
    css = (root / "static/css/app.css").read_text(encoding="utf-8")
    assert ".htmx-indicator" in css
    assert ".spinner" in css

    for name in ("partials/email_table.html", "partials/metrics.html"):
        markup = (root / "templates" / name).read_text(encoding="utf-8")
        assert "htmx-indicator" in markup, name
        assert "spinner" in markup, name


# ---------------------------------------------------- accessibility floor ---

# `docs/design/system.md` calls these non-negotiable and every one of them was
# already true, apart from the skip link. They are asserted rather than left as
# prose because each is the kind of thing a single later template quietly
# breaks - and nothing else in the suite would notice.


def _templates() -> list[tuple[str, str]]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/api/src/seskit_api/templates"
    return [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.html"))
    ]


def _stylesheet() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/api/src/seskit_api"
    return (root / "static/css/app.css").read_text(encoding="utf-8")


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


def test_every_control_reads_its_height_from_the_scale() -> None:
    """There were five heights: a 32px button, a 34px input, a 30px select, a
    36px auth submit and a 26px small button.

    That is what "the spacing looks off" turned out to mean. Every
    `row row--end` form on the dashboard bottom-aligns a button against an
    input, so two of those sat side by side with the button standing 2px proud
    of the field it belonged to.

    Asserted on the tokens rather than on the numbers, because the point is not
    which pixel value won - it is that a sixth one cannot be added without
    saying so here first.
    """
    css = _stylesheet()

    for token in ("--control-h-sm:", "--control-h:", "--control-h-lg:"):
        assert token in css, f"{token} is not defined"

    for selector in (".btn", ".input", ".btn--sm", ".auth__submit"):
        declarations = _rule(css, selector)
        assert "height:" in declarations, selector
        assert "var(--control-h" in declarations, (
            f"{selector} sets a height that is not on the scale: {declarations.strip()}"
        )


def test_a_select_is_styled_as_a_select() -> None:
    """Both dropdowns wore `.input` - a rule written for a text field.

    Nothing fails when a <select> borrows it. The browser simply paints its own
    chevron over the padding meant for text and sizes the control however it
    likes, which is why the region picker and the project switcher matched
    neither each other nor the fields beside them.
    """
    css = _stylesheet()

    assert ".select {" in css
    assert "appearance: none" in _rule(css, ".select")

    borrowed = [
        name
        for name, markup in _templates()
        if any('class="input' in tag for tag in re.findall(r"<select[^>]*>", markup))
    ]

    assert not borrowed, f"a <select> is wearing .input in: {borrowed}"


def test_the_chevron_is_drawn_for_both_themes() -> None:
    """A data URI cannot read a custom property, so its stroke colour is baked
    in and each theme needs its own. Miss one and the arrow is there but
    invisible - the failure that looks like nothing at all.
    """
    css = _stylesheet()

    assert css.count("--select-chevron:") == 3, (
        "one per theme block: :root, the prefers-color-scheme override, and the "
        "explicit [data-theme='dark']"
    )


def test_every_container_that_holds_page_content_spaces_it() -> None:
    """The reset sets `* { margin: 0 }`, so a container that only pads is a
    container whose children touch.

    `.card__body` was exactly that. Every card holding more than one thing was
    affected, and the Webhooks page was the worst of them: a description list,
    a row of three buttons, a paragraph, a heading and a table, all flush.

    Asserted on the containers rather than on the pages, because the pages were
    not doing anything wrong. A page should be able to put two elements in a
    card without knowing that it has to space them itself.
    """
    css = _stylesheet()

    for selector in (".content", ".stack", ".card__body", ".auth__card"):
        assert "gap:" in _rule(css, selector), (
            f"{selector} holds arbitrary content and supplies no spacing of its own"
        )


def test_the_six_counts_are_one_grid_of_equal_tiles() -> None:
    """They were two grids - four tiles, then two.

    `.grid-metrics` sizes its columns against the width it is given, so the
    second grid laid its pair out across the whole row: the last two tiles came
    out twice as wide as the four above them, on the first screen of the
    dashboard.

    `auto-fill` rather than `auto-fit` is the other half. auto-fit collapses
    the empty tracks on a part-full last row and gives their width away, which
    reintroduces the same two sizes at any window where six does not divide by
    the column count.
    """
    metrics = dict(_templates())["partials/metrics.html"]

    assert metrics.count('class="grid-metrics"') == 1
    assert "auto-fill" in _stylesheet()
    assert "auto-fit" not in _rule(_stylesheet(), ".grid-metrics")


def test_no_navigation_is_taken_away_on_a_small_screen() -> None:
    """`.sidebar__footer` is Settings and Docs. It was `display: none` under
    900px, and nothing else on any page links to either, so on a phone those
    two pages could not be reached at all.

    The section label above the nav is a different case and stays hidden: a
    heading over a horizontal bar labels nothing.
    """
    import re

    css = _stylesheet()

    assert not re.search(r"\.sidebar__footer[^{}]*\{[^{}]*display:\s*none", css)
    assert not re.search(r"\.sidebar__nav[^{}]*\{[^{}]*display:\s*none", css)


def test_no_table_can_scroll_the_page_sideways() -> None:
    """Wide content scrolls inside `.table-wrap`; the body never does.

    A table that widens the document breaks every other page on a phone, not
    just its own.
    """
    import re

    unwrapped: list[str] = []
    for name, markup in _templates():
        for match in re.finditer(r"<table\b", markup):
            if "table-wrap" not in markup[: match.start()].rsplit("<div", 1)[-1]:
                unwrapped.append(name)

    assert not unwrapped, f"tables not inside .table-wrap: {unwrapped}"


def test_a_badge_that_means_something_by_colour_also_says_it_by_shape() -> None:
    """State is encoded in form as well as hue - the dot is the form.

    `dot=False` is legitimate for a badge with no tone, which is a plain label
    carrying no colour meaning. It is not legitimate on a toned one, because
    then the colour is the only thing saying what the badge means.
    """
    import re

    offenders: list[str] = []
    for name, markup in _templates():
        for call in re.findall(r"badge\((.*?)\)", markup, re.DOTALL):
            if "dot=False" in call and "tone=" in call:
                offenders.append(f"{name}: badge({call.strip()[:60]}...)")

    assert not offenders, "toned badges relying on colour alone:\n" + "\n".join(offenders)


def test_there_is_one_focus_treatment_and_it_is_not_switched_off() -> None:
    css = _stylesheet()

    assert ":focus-visible {" in css
    assert "outline:" in css.split(":focus-visible {", 1)[1][:200]
    # Removing the outline for mouse focus is fine; removing it outright is the
    # regression this guards.
    assert ":focus:not(:focus-visible)" in css


def test_the_active_nav_item_is_marked_for_assistive_technology() -> None:
    markup = dict(_templates())["base.html"]

    assert 'aria-current="page"' in markup


# --------------------------------------------------------------- branding ---


def test_the_brand_mark_is_the_transparent_asset() -> None:
    """Only the icon has an alpha channel.

    The supplied horizontal and stacked wordmarks are RGB with a baked white
    background, so either of them in the app shell would render as a white
    block in dark mode. This fails if one is ever wired into a template.
    """
    banned = ("logo-horizontal", "logo_horizontal", "logo-stacked", "logo_stacked")
    offenders = [
        f"{name}: {word}" for name, markup in _templates() for word in banned if word in markup
    ]

    assert not offenders, "opaque wordmark in the app shell:\n" + "\n".join(offenders)


def test_the_accent_and_the_focus_ring_agree_in_every_theme() -> None:
    """The ring is the accent by definition.

    There are three theme blocks - :root, the prefers-color-scheme media query,
    and [data-theme] for the explicit toggle - and moving the accent in two of
    them is exactly the mistake this catches.
    """
    import re

    css = _stylesheet()
    accents = re.findall(r"--accent:\s*(#[0-9a-f]{6})", css)
    rings = re.findall(r"--focus-ring:\s*(#[0-9a-f]{6})", css)

    assert len(accents) == 3, f"expected three theme blocks, found {len(accents)}"
    assert set(rings) <= set(accents), f"focus rings {set(rings)} not among accents {set(accents)}"


def test_every_page_offers_a_favicon() -> None:
    """Both shells, not just the dashboard. The sign-in page is the first thing
    a new instance shows, and a default globe in the tab there is the first
    impression.
    """
    shells = dict(_templates())

    for name in ("base.html", "base_auth.html"):
        assert 'rel="icon"' in shells[name], name
        assert 'rel="apple-touch-icon"' in shells[name], name


# ----------------------------------------------------------- the skip link ---


async def test_the_skip_link_can_be_seen_when_focused(signed_in_client: AsyncClient) -> None:
    """It was in the DOM already, wearing `.visually-hidden` - which has no
    `:focus` escape, so a sighted keyboard user tabbed to it and saw nothing.

    `.visually-hidden` must not grow one either: it is also what hides the
    "View " prefix inside the Emails table links.
    """
    page = await signed_in_client.get("/")

    assert 'class="skip-link" href="#main"' in page.text
    assert 'id="main"' in page.text


# ------------------------------------------------------- assets and proxies ---


def test_no_template_builds_an_absolute_asset_url() -> None:
    """`url_for` returns a URL with a scheme and host, built from what the
    application believes the request was.

    Behind a proxy that terminates TLS - every managed platform - the app sees
    plain HTTP unless it has been told to trust `X-Forwarded-Proto`. It then
    serves an HTTPS page whose stylesheet is an `http://` URL, the browser
    refuses it as mixed content, and the CSP refuses it too because a different
    scheme is a different origin.

    The result is a dashboard with no styling whatsoever and nothing in the log
    to say why. A deployment found it; no test could have, because everything
    here is served over HTTP.

    `static()` returns a root-relative URL instead, which cannot carry the
    wrong scheme because it carries none.
    """
    offenders = [name for name, markup in _templates() if "url_for(" in markup]

    assert not offenders, (
        f"templates building absolute asset URLs: {offenders} - use static() instead"
    )


def test_the_static_helper_is_root_relative_and_keeps_a_sub_path() -> None:
    """The one thing `url_for` was doing here that mattered is the `root_path`
    prefix, for an instance mounted somewhere other than the root.
    """
    from seskit_api.templating import static
    from starlette.requests import Request

    def _request(root: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "root_path": root,
                "scheme": "http",
                "server": ("example", 80),
                "query_string": b"",
            }
        )

    assert static({"request": _request("")}, "/css/app.css") == "/static/css/app.css"
    # A leading slash on the argument is optional, not load-bearing.
    assert static({"request": _request("")}, "css/app.css") == "/static/css/app.css"
    assert static({"request": _request("/seskit")}, "/css/app.css") == (
        "/seskit/static/css/app.css"
    )
