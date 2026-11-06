"""A page per domain, over HTTP.

What is worth asserting is the reason the pages exist: **one domain's page shows
that domain's mail and nothing else.** Every tab is checked against a second
domain in the same project, and against another project's domain, because a
filter that works when there is one domain proves nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from seskit_core.models import Email, EmailStatus, Identity, Project
from seskit_core.services import create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession
from test_domains_page import _connect, _sign_in
from test_domains_receiving import _domain, _project
from test_v1_inbound import _message

TABS = ("", "/emails", "/inbox", "/setup")


async def _two_domains(
    client: AsyncClient, session: AsyncSession
) -> tuple[Project, Identity, Identity]:
    await _connect(client)
    project = await _project(session)
    first = await _domain(session, project, "alpha.example")
    second = await _domain(session, project, "beta.example")
    return project, first, second


def _sent(project: Project, sender: str, subject: str, n: int) -> Email:
    return Email(
        project_id=project.id,
        from_address=sender,
        to_addresses=["user@example.org"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject=subject,
        text_body="Hi",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id=f"ses-{n}",
        sent_at=datetime.now(UTC) - timedelta(minutes=5),
    )


# ------------------------------------------------------------------ access ---


@pytest.mark.parametrize("tab", TABS)
async def test_a_domain_page_needs_a_session(app_client: AsyncClient, tab: str) -> None:
    response = await app_client.get(f"/domains/dom_x{tab}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("tab", TABS)
async def test_an_unknown_domain_is_not_found(app_client: AsyncClient, tab: str) -> None:
    await _sign_in(app_client)

    assert (await app_client.get(f"/domains/dom_nope{tab}")).status_code == 404


@pytest.mark.parametrize("tab", TABS)
async def test_another_projects_domain_is_the_same_not_found(
    app_client: AsyncClient, db_session: AsyncSession, tab: str
) -> None:
    """An id from somebody else's project answers exactly as one that does not
    exist, so nobody can probe for real ids."""
    await _sign_in(app_client)
    stranger = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    theirs = await create_project(db_session, user_id=stranger.id, name="Theirs")
    identity = await _domain(db_session, theirs, "theirs.example")

    assert (await app_client.get(f"/domains/{identity.id}{tab}")).status_code == 404


# ------------------------------------------------------------------- frame ---


async def test_every_tab_names_the_domain_and_links_the_others(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, _ = await _two_domains(app_client, db_session)

    for tab in TABS:
        page = await app_client.get(f"/domains/{first.id}{tab}")

        assert page.status_code == 200, tab
        assert "<h1>alpha.example</h1>" in page.text
        for other in ("", "/emails", "/inbox", "/setup"):
            assert f'href="/domains/{first.id}{other}"' in page.text


async def test_the_domains_list_leads_to_each_domain(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, second = await _two_domains(app_client, db_session)

    page = await app_client.get("/domains")

    assert f'href="/domains/{first.id}"' in page.text
    assert f'href="/domains/{second.id}"' in page.text


async def test_an_address_has_no_inbox(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """An address has no MX record to receive at. Offering an Inbox would suggest
    mail could arrive, and the URL answers 404 rather than an empty page."""
    await _connect(app_client)
    address = await _domain(
        db_session, await _project(db_session), "me@example.com", kind="email_address"
    )

    overview = await app_client.get(f"/domains/{address.id}")
    inbox = await app_client.get(f"/domains/{address.id}/inbox")

    assert overview.status_code == 200
    assert f'href="/domains/{address.id}/inbox"' not in overview.text
    assert inbox.status_code == 404


# ------------------------------------------------------------------ emails ---


async def test_the_emails_tab_lists_only_that_domains_mail(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project, first, second = await _two_domains(app_client, db_session)
    mine = _sent(project, "hello@alpha.example", "From alpha", 1)
    theirs = _sent(project, "hello@beta.example", "From beta", 2)
    db_session.add_all([mine, theirs])
    await db_session.flush()

    alpha = await app_client.get(f"/domains/{first.id}/emails")
    beta = await app_client.get(f"/domains/{second.id}/emails")

    assert mine.id in alpha.text and theirs.id not in alpha.text
    assert theirs.id in beta.text and mine.id not in beta.text
    assert "1 message sent" in alpha.text


async def test_a_subdomain_belongs_to_the_most_specific_identity(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`mail.alpha.example` was added, so its mail is its own and not alpha's."""
    project, first, _ = await _two_domains(app_client, db_session)
    sub = await _domain(db_session, project, "mail.alpha.example")
    parent_mail = _sent(project, "hello@alpha.example", "Parent", 1)
    sub_mail = _sent(project, "hello@mail.alpha.example", "Child", 2)
    unclaimed = _sent(project, "hello@news.alpha.example", "Unclaimed", 3)
    db_session.add_all([parent_mail, sub_mail, unclaimed])
    await db_session.flush()

    parent_page = await app_client.get(f"/domains/{first.id}/emails")
    child_page = await app_client.get(f"/domains/{sub.id}/emails")

    assert parent_mail.id in parent_page.text
    assert unclaimed.id in parent_page.text
    assert sub_mail.id not in parent_page.text
    assert sub_mail.id in child_page.text
    assert parent_mail.id not in child_page.text


async def test_the_emails_filter_stays_on_the_domain(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The status links must point back at this domain, not at the global list."""
    _, first, _ = await _two_domains(app_client, db_session)

    page = await app_client.get(f"/domains/{first.id}/emails")

    assert f'href="/domains/{first.id}/emails?status=sent"' in page.text
    assert 'href="/emails?status=' not in page.text
    fragment = await app_client.get(f"/domains/{first.id}/partials/emails?status=sent")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text


# ---------------------------------------------------------------- overview ---


def _sent_tile(page: str) -> str:
    """The value under the "Sent" tile, so an assertion reads a number and not a
    substring that could appear anywhere on the page."""
    match = re.search(r'metric__label">Sent</span>\s*<span class="metric__value">([^<]*)<', page)
    assert match, "no Sent tile on the page"
    return match.group(1).strip()


async def test_the_overview_counts_only_that_domains_mail(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project, first, second = await _two_domains(app_client, db_session)
    db_session.add_all(
        [
            _sent(project, "hello@alpha.example", "a", 1),
            _sent(project, "hello@beta.example", "b", 2),
            _sent(project, "hello@beta.example", "c", 3),
        ]
    )
    await db_session.flush()

    quiet = await app_client.get(f"/domains/{first.id}")
    busy = await app_client.get(f"/domains/{second.id}")
    everything = await app_client.get("/")

    assert _sent_tile(quiet.text) == "1"
    assert _sent_tile(busy.text) == "2"
    assert _sent_tile(everything.text) == "3"


async def test_the_overview_range_control_stays_on_the_domain(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project, first, _ = await _two_domains(app_client, db_session)
    db_session.add(_sent(project, "hello@alpha.example", "a", 1))
    await db_session.flush()

    page = await app_client.get(f"/domains/{first.id}")
    fragment = await app_client.get(f"/domains/{first.id}/partials/metrics?range=7d")

    assert f'href="/domains/{first.id}?range=7d"' in page.text
    assert 'href="/?range=' not in page.text
    assert fragment.status_code == 200
    assert "<html" not in fragment.text


async def test_a_domain_with_no_mail_says_so(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, _ = await _two_domains(app_client, db_session)

    page = await app_client.get(f"/domains/{first.id}")

    assert "Nothing sent from alpha.example" in page.text


# ------------------------------------------------------------------- inbox ---


async def test_the_inbox_tab_shows_only_that_domains_received_mail(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project, first, second = await _two_domains(app_client, db_session)
    first.inbound_rule_name = "rule-alpha"
    second.inbound_rule_name = "rule-beta"
    await _message(
        db_session, project_id=project.id, second=1, domain="alpha.example", subject="To alpha"
    )
    await _message(
        db_session, project_id=project.id, second=2, domain="beta.example", subject="To beta"
    )

    alpha = await app_client.get(f"/domains/{first.id}/inbox")
    beta = await app_client.get(f"/domains/{second.id}/inbox")

    assert "To alpha" in alpha.text and "To beta" not in alpha.text
    assert "To beta" in beta.text and "To alpha" not in beta.text
    # This page is the filter, so it does not offer one.
    assert 'aria-label="Filter by domain"' not in alpha.text


async def test_a_domain_that_is_not_receiving_says_what_to_do(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, _ = await _two_domains(app_client, db_session)

    page = await app_client.get(f"/domains/{first.id}/inbox")

    assert page.status_code == 200
    assert "Not receiving mail for alpha.example" in page.text
    assert f'href="/domains/{first.id}/setup"' in page.text


async def test_a_receiving_domain_with_no_mail_says_it_is_waiting(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, _ = await _two_domains(app_client, db_session)
    first.inbound_rule_name = "rule-alpha"
    await db_session.flush()

    page = await app_client.get(f"/domains/{first.id}/inbox")

    assert "Waiting for the first message" in page.text
    assert "alpha.example" in page.text


# ------------------------------------------------------------------- setup ---


async def test_the_setup_tab_is_the_domains_card(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, first, _ = await _two_domains(app_client, db_session)

    page = await app_client.get(f"/domains/{first.id}/setup")

    assert "Verification" in page.text
    assert f'action="/domains/{first.id}/refresh"' in page.text
    assert "csrf_token" in page.text
    # Already on the domain's page, so the card does not link to itself.
    assert f'href="/domains/{first.id}"' in page.text  # the Overview tab
    assert ">Open<" not in page.text


async def test_the_domains_list_shows_each_domains_own_count(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project, _, _ = await _two_domains(app_client, db_session)
    db_session.add_all(
        [
            _sent(project, "hello@alpha.example", "a", 1),
            _sent(project, "hello@beta.example", "b", 2),
            _sent(project, "hello@beta.example", "c", 3),
        ]
    )
    await db_session.flush()

    page = await app_client.get("/domains")

    assert "1 sent" in page.text
    assert "2 sent" in page.text
