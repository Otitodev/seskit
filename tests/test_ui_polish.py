"""Action feedback (§17, `docs/design-system.md`).

Until this, every state-changing form in the dashboard re-rendered its page and
said nothing. A successful save looked exactly like a save that never happened,
which is the failure mode that makes a user press the button again.

The test that matters here is the escaping one. Two confirmations quote input
the user typed - an identity value and an API key name - so the flash is a path
from a form field to rendered HTML, and it needs holding to the same standard
as the email body on the Emails page.
"""

from __future__ import annotations

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

# `design-system.md` calls these non-negotiable and every one of them was
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
