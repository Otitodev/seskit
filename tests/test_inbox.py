"""The Inbox, over HTTP (inbound email, Phase D).

What a signed-in person sees of mail somebody sent to them. The thing worth
asserting over and over is the same: **everything on these pages was written by a
stranger.** Every sender-controlled field is fed a script tag and the assertion is
that it is never in the response as markup - the page a signed-in person looks at
holds their session, and a stranger's HTML does not belong next to it.

The second thing is that a failed download lands on the page the person was looking
at and says why, instead of a bare JSON envelope in a browser tab.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes.inbound import FakeInboundStore
from httpx import AsyncClient
from seskit_core.models import InboundEmail, Project
from seskit_core.services import create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession
from test_domains_page import _connect, _sign_in
from test_domains_receiving import _domain, _project
from test_v1_inbound import _message
from test_v1_inbound_downloads import ATTACHMENTS, HTML_ATTACHMENT, PDF, _raw

HOSTILE = "<script>alert(document.cookie)</script><img src=x onerror=alert(1)>"


async def _seed(session: AsyncSession, project: Project, **overrides: Any) -> InboundEmail:
    return await _message(session, project_id=project.id, **overrides)


async def _signed_in_with_project(
    client: AsyncClient, session: AsyncSession
) -> tuple[str, Project]:
    token = await _connect(client)
    return token, await _project(session)


def _never_as_markup(text: str) -> None:
    assert "<script>alert(document.cookie)" not in text
    assert "onerror=alert(1)>" not in text


# ------------------------------------------------------------------ access ---


@pytest.mark.parametrize("path", ["/inbox", "/partials/inbox", "/inbox/inbound_x"])
async def test_the_inbox_needs_a_session(app_client: AsyncClient, path: str) -> None:
    response = await app_client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_nav_offers_the_inbox(app_client: AsyncClient) -> None:
    await _sign_in(app_client)

    page = await app_client.get("/domains")

    assert 'href="/inbox"' in page.text


# -------------------------------------------------------------------- list ---


async def test_a_project_that_never_turned_receiving_on_is_told_what_to_do(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connect(app_client)

    page = await app_client.get("/inbox")

    assert page.status_code == 200
    assert "Not receiving mail yet" in page.text
    assert 'href="/domains"' in page.text


async def test_a_project_receiving_with_nothing_yet_says_it_is_waiting(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")

    page = await app_client.get("/inbox")

    assert "Waiting for the first message" in page.text
    assert "example.com" in page.text
    assert "MX record" in page.text


async def test_mail_is_listed_newest_first_with_the_subject_as_the_link(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")
    older = await _seed(db_session, project, second=1, subject="the older one")
    newer = await _seed(db_session, project, second=2, subject="the newer one")

    page = await app_client.get("/inbox")

    assert page.text.index("the newer one") < page.text.index("the older one")
    assert f'href="/inbox/{newer.id}"' in page.text
    assert f'href="/inbox/{older.id}"' in page.text


async def test_nothing_a_stranger_wrote_reaches_the_list_as_markup(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")
    await _seed(db_session, project, subject=HOSTILE, from_name=HOSTILE, from_address="x@y.z")

    for path in ("/inbox", "/partials/inbox"):
        page = await app_client.get(path)

        _never_as_markup(page.text)
        assert "&lt;script&gt;" in page.text


async def test_a_message_with_no_subject_or_sender_still_has_something_to_click(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")
    message = await _seed(db_session, project, subject="", from_address="", from_name="")

    page = await app_client.get("/inbox")

    assert "(no subject)" in page.text
    assert "unknown sender" in page.text
    assert f'href="/inbox/{message.id}"' in page.text


async def test_another_projects_mail_is_never_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, mine = await _signed_in_with_project(app_client, db_session)
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    theirs = await create_project(db_session, user_id=other_user.id, name="Other")
    await _seed(db_session, mine, subject="mine", second=1)
    await _seed(db_session, theirs, subject="theirs", second=2)

    page = await app_client.get("/inbox")

    assert "mine" in page.text
    assert "theirs" not in page.text


async def test_the_domain_filter_narrows_and_an_unknown_one_shows_everything(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, "a.example", rule="seskit-a")
    await _domain(db_session, project, "b.example", rule="seskit-b")
    await _seed(db_session, project, domain="a.example", subject="for a", second=1)
    await _seed(db_session, project, domain="b.example", subject="for b", second=2)

    narrowed = await app_client.get("/inbox", params={"domain": "b.example"})
    unknown = await app_client.get("/inbox", params={"domain": "nope.example"})

    assert "for b" in narrowed.text and "for a" not in narrowed.text
    # A stale or hand-edited URL renders the page it was clearly asking for.
    assert "for a" in unknown.text and "for b" in unknown.text


async def test_older_mail_is_reached_by_cursor_without_skipping_or_repeating(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")
    for n in range(52):
        await _seed(db_session, project, second=n, subject=f"message-{n:02d}")

    first = await app_client.get("/inbox")

    assert "Older messages" in first.text
    assert "message-51" in first.text
    assert "message-01" not in first.text
    marker = 'href="/inbox?before='
    start = first.text.index(marker) + len('href="')
    older_link = first.text[start : first.text.index('"', start)].replace("&amp;", "&")
    second = await app_client.get(older_link)
    assert "message-01" in second.text
    assert "message-00" in second.text
    assert "message-51" not in second.text
    assert "Older messages" not in second.text


async def test_the_fragment_is_the_table_alone(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    await _domain(db_session, project, rule="seskit-x")
    await _seed(db_session, project, subject="hello")

    fragment = await app_client.get("/partials/inbox")

    assert 'id="inbox-table"' in fragment.text
    assert "<html" not in fragment.text


# ------------------------------------------------------------------ detail ---


async def test_one_message_shows_what_was_received_and_what_ses_concluded(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(
        db_session,
        project,
        envelope_to=["support@example.com", "bcc@example.com"],
        spf_verdict="PASS",
        dkim_verdict="GRAY",
        dmarc_verdict=None,
        spam_verdict="FAIL",
        text_body="the plain body",
    )

    page = await app_client.get(f"/inbox/{message.id}")

    assert page.status_code == 200
    assert "Ada Lovelace" in page.text
    assert "ada@other.org" in page.text
    assert "the plain body" in page.text
    # A blind copy appears under "Delivered to" and nowhere in the headers.
    assert "bcc@example.com" in page.text
    assert "Pass" in page.text
    assert "Inconclusive" in page.text
    assert "Fail" in page.text
    assert "SESKit shows them and acts on none" in page.text


async def test_a_verdict_ses_did_not_give_reads_not_checked_never_pass(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(
        db_session,
        project,
        spf_verdict=None,
        dkim_verdict=None,
        dmarc_verdict=None,
        spam_verdict=None,
        virus_verdict=None,
    )

    page = await app_client.get(f"/inbox/{message.id}")

    # Five verdict rows. Counted as rows and not as the phrase, which the page's own
    # explanation also uses once.
    assert page.text.count('<span class="muted">Not checked</span>') == 5
    assert "Pass" not in page.text


async def test_nothing_a_stranger_wrote_reaches_the_detail_page_as_markup(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Every field a sender controls, each carrying a script tag. The page holds a
    signed-in person's session; none of it may be executable.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(
        db_session,
        project,
        subject=HOSTILE,
        from_name=HOSTILE,
        text_body=HOSTILE,
        html_body=HOSTILE,
        message_id_header=HOSTILE,
        in_reply_to=HOSTILE,
        to_addresses=[HOSTILE],
        cc_addresses=[HOSTILE],
        reply_to=[HOSTILE],
        envelope_to=[HOSTILE],
        attachments=[
            {
                "index": 0,
                "filename": HOSTILE,
                "content_type": "text/html",
                "size": 10,
                "inline": False,
                "content_id": None,
            }
        ],
        headers=[["X-Evil", HOSTILE], [HOSTILE, "v"]],
    )

    page = await app_client.get(f"/inbox/{message.id}")

    _never_as_markup(page.text)
    assert page.text.count("&lt;script&gt;") >= 8


async def test_the_senders_html_is_never_written_into_the_page(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The message's HTML reaches the browser as a document of its own, in a frame.
    It is never part of this page: not inlined, not in a srcdoc, and the sender's
    own elements - including an iframe of theirs - exist only as escaped text.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(
        db_session, project, html_body='<p id="pwned">x</p><iframe src="//evil"></iframe>'
    )

    page = await app_client.get(f"/inbox/{message.id}")

    assert '<p id="pwned">' not in page.text
    assert "srcdoc" not in page.text
    # Exactly one frame exists, and it is ours: the sender's is only visible text.
    assert page.text.count("<iframe") == 1
    assert '<iframe src="//evil"' not in page.text
    assert "&lt;p id=" in page.text
    assert "&lt;iframe" in page.text


async def test_the_preview_frame_is_sandboxed_with_no_permissions_at_all(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """``sandbox`` with no tokens. ``allow-scripts`` together with
    ``allow-same-origin`` would let the frame remove its own sandbox, so neither - and
    nothing else - may ever be granted.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project)

    page = await app_client.get(f"/inbox/{message.id}")

    (frame,) = re.findall(r"<iframe[^>]*>", page.text)
    assert 'sandbox=""' in frame
    assert f'src="/inbox/{message.id}/html"' in frame
    assert "allow-" not in frame


async def test_a_message_with_no_html_has_no_frame(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project, html_body="")

    page = await app_client.get(f"/inbox/{message.id}")

    assert "<iframe" not in page.text


async def test_adding_the_frame_did_not_loosen_the_page_that_holds_it(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The page that frames a stranger's document is the one holding the session.
    It must still refuse to be framed itself and keep its own policy.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project)

    response = await app_client.get(f"/inbox/{message.id}")

    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self' 'nonce-" in response.headers["content-security-policy"]
    assert "sandbox" not in response.headers["content-security-policy"]


# --------------------------------------------------- the HTML as a document ---


async def test_the_html_is_served_exactly_as_sent_inside_a_tight_policy(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Not cleaned: a sanitiser is one more thing to get wrong. What contains it is
    the policy, and a test that the bytes arrive unaltered is also a test that nobody
    quietly added one.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    hostile = '<html><body><script>alert(document.cookie)</script><img src="http://t.example/p.gif"><form action="http://evil.example"><input name=pw></form></body></html>'
    message = await _seed(db_session, project, html_body=hostile)

    response = await app_client.get(f"/inbox/{message.id}/html")

    assert response.status_code == 200
    assert response.text == hostile
    assert response.headers["content-type"] == "text/html; charset=utf-8"


async def test_the_policy_on_the_html_blocks_script_network_forms_and_navigation(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project)

    response = await app_client.get(f"/inbox/{message.id}/html")
    policy = response.headers["content-security-policy"]
    directives = {part.strip().split(" ", 1)[0]: part.strip() for part in policy.split(";")}

    # `sandbox` with no tokens: opaque origin, no script, no forms, no navigation.
    assert directives["sandbox"] == "sandbox"
    # Nothing loads from the network, which blocks tracking pixels and remote images.
    assert directives["default-src"] == "default-src 'none'"
    assert directives["img-src"] == "img-src data:"
    # Inline styles are the one thing allowed; without them most mail is unreadable.
    assert directives["style-src"] == "style-src 'unsafe-inline'"
    assert "script-src" not in directives
    assert "unsafe-eval" not in policy
    assert "'self'" not in policy.replace("frame-ancestors 'self'", "")


async def test_only_the_message_page_may_frame_the_html(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The middleware used to force every response to DENY. This is the one that has
    to be framed, by the same origin and nobody else.
    """
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project)

    response = await app_client.get(f"/inbox/{message.id}/html")

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "private, no-store"


async def test_a_message_with_no_html_says_so_on_its_own_page(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(db_session, project, html_body="")

    response = await app_client.get(f"/inbox/{message.id}/html")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "no HTML body" in response.text


async def test_the_html_route_gives_a_stranger_nothing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Another project's id and one that never existed answer the same, and without
    a session there is nothing at all.
    """
    await _signed_in_with_project(app_client, db_session)
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    theirs = await create_project(db_session, user_id=other_user.id, name="Other")
    foreign = await _seed(db_session, theirs, html_body="<p>secret</p>")

    a = await app_client.get(f"/inbox/{foreign.id}/html", follow_redirects=False)
    b = await app_client.get("/inbox/inbound_01NEVEREXISTED/html", follow_redirects=False)

    assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location"))
    assert a.status_code == 303
    assert "secret" not in a.text


async def test_another_projects_message_and_an_unknown_one_look_identical(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The same answer either way, so a stranger cannot probe for real ids."""
    await _signed_in_with_project(app_client, db_session)
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    theirs = await create_project(db_session, user_id=other_user.id, name="Other")
    foreign = await _seed(db_session, theirs)

    for suffix in ("", "/raw", "/attachments/0"):
        a = await app_client.get(f"/inbox/{foreign.id}{suffix}", follow_redirects=False)
        b = await app_client.get(f"/inbox/inbound_01NEVEREXISTED{suffix}", follow_redirects=False)

        assert (a.status_code, a.headers.get("location")) == (
            b.status_code,
            b.headers.get("location"),
        )
        assert a.status_code == 303


async def test_an_unreadable_message_says_so_and_still_has_its_original(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, project = await _signed_in_with_project(app_client, db_session)
    message = await _seed(
        db_session,
        project,
        parse_failed=True,
        text_body="",
        html_body="",
        subject="",
        from_address="",
    )

    page = await app_client.get(f"/inbox/{message.id}")

    assert "could not read this message" in page.text
    assert "Download .eml" in page.text


# --------------------------------------------------------------- downloads ---


async def _stored(
    client: AsyncClient,
    session: AsyncSession,
    store: FakeInboundStore,
    **overrides: Any,
) -> InboundEmail:
    _, project = await _signed_in_with_project(client, session)
    fields: dict[str, Any] = {"attachments": ATTACHMENTS, **overrides}
    message = await _seed(session, project, **fields)
    store.put(message.storage_bucket, message.storage_key, _raw())
    return message


async def test_an_attachment_downloads_as_opaque_bytes_in_a_sandbox(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    message = await _stored(app_client, db_session, inbound_store)

    response = await app_client.get(f"/inbox/{message.id}/attachments/0")

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith('attachment; filename="report.pdf"')
    assert response.headers["x-content-type-options"] == "nosniff"
    # The header the route set, not the dashboard's: the middleware keeps it.
    assert "sandbox" in response.headers["content-security-policy"]
    assert "script-src" not in response.headers["content-security-policy"]


async def test_a_file_that_declares_itself_html_is_still_served_as_a_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    message = await _stored(app_client, db_session, inbound_store)

    response = await app_client.get(f"/inbox/{message.id}/attachments/1")

    assert response.content == HTML_ATTACHMENT
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "set-cookie" not in {name.lower() for name in response.headers}


async def test_the_original_downloads_as_an_eml(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    message = await _stored(app_client, db_session, inbound_store)

    response = await app_client.get(f"/inbox/{message.id}/raw")

    assert response.content == _raw()
    assert response.headers["content-type"] == "message/rfc822"
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="{message.id}.eml"'
    )
    assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("suffix", ["/attachments/0", "/raw"])
async def test_a_message_past_retention_explains_itself_on_the_page_and_fetches_nothing(
    app_client: AsyncClient,
    db_session: AsyncSession,
    inbound_store: FakeInboundStore,
    suffix: str,
) -> None:
    """A page and not a JSON envelope: this is a link in a browser, and a bare
    error object in a tab tells a person nothing.
    """
    message = await _stored(
        app_client,
        db_session,
        inbound_store,
        raw_expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    response = await app_client.get(f"/inbox/{message.id}{suffix}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "no longer in storage" in response.text
    assert message.subject in response.text
    assert inbound_store.fetches == []


async def test_an_attachment_the_message_does_not_have_is_a_page_not_a_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    message = await _stored(app_client, db_session, inbound_store)

    response = await app_client.get(f"/inbox/{message.id}/attachments/9")

    assert response.status_code == 404
    assert "no attachment with that index" in response.text
    assert inbound_store.fetches == []


async def test_a_permission_failure_is_reported_as_one(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """A missing s3:GetObject must not read as "your mail is gone"."""
    from seskit_core.errors import APIError, ErrorType

    message = await _stored(app_client, db_session, inbound_store)
    inbound_store.error = APIError(
        ErrorType.AUTHORIZATION_FAILED, "The AWS identity is not permitted to call s3:GetObject."
    )

    response = await app_client.get(f"/inbox/{message.id}/raw")

    assert response.status_code == 400
    assert "not permitted to call s3:GetObject" in response.text


async def test_an_expired_original_does_not_offer_download_links(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    message = await _stored(
        app_client,
        db_session,
        inbound_store,
        raw_expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    page = await app_client.get(f"/inbox/{message.id}")

    assert "/attachments/" not in page.text
    assert f"/inbox/{message.id}/raw" not in page.text
    assert "retention period" in page.text
    # The parsed message stays readable.
    assert "Hi." in page.text
