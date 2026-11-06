"""A domain's page, rendered from the real templates.

Needs no database: each tab is rendered with a plain ``Identity`` and the real
component layer, the way the other page tests that read markup do. What the HTTP
tests in ``test_domain_pages.py`` cannot cheaply show, a template that does not
compile or a link that points at the global page instead of the domain's, fails
here first.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from seskit_api.templating import templates
from seskit_core.models import EmailStatus, Identity
from seskit_core.services import TimeRange
from starlette.requests import Request

REQUEST = Request(
    {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
)

METRICS = SimpleNamespace(
    range=TimeRange.DAY,
    has_activity=True,
    sent=1,
    delivered=1,
    bounced=0,
    complained=0,
    opened=0,
    clicked=0,
    tracking_enabled=False,
    delivery_rate=1.0,
    bounce_rate=0.0,
    complaint_rate=0.0,
    open_rate=None,
    click_rate=None,
)


def _identity(**overrides: object) -> Identity:
    fields: dict[str, object] = {
        "id": "dom_1",
        "project_id": "proj_1",
        "identity_type": "domain",
        "value": "alpha.example",
        "region": "us-east-1",
        "verification_status": "success",
        "dkim_status": "success",
        "dkim_tokens": [],
    }
    return Identity(**(fields | overrides))


def _render(name: str, identity: Identity, tab: str, **context: Any) -> str:
    tabs = (
        ("overview", "emails", "inbox", "setup")
        if identity.is_domain
        else (
            "overview",
            "emails",
            "setup",
        )
    )
    base: dict[str, Any] = {
        "request": REQUEST,
        "identity": identity,
        "project": SimpleNamespace(name="Project"),
        "tab": tab,
        "tabs": tabs,
        "base": f"/domains/{identity.id}",
        "csrf_token": "token",
        "csp_nonce": "nonce",
        "current": SimpleNamespace(user=SimpleNamespace(email="a@b.c"), csrf_token="token"),
        "projects": [],
        "nav_active": "domains",
        "flash": None,
        "error": None,
    }
    return templates.env.get_template(name).render(**(base | context))


def _overview(identity: Identity, *, active: bool = True, total: int = 1) -> str:
    return _render(
        "pages/domain_overview.html",
        identity,
        "overview",
        metrics=METRICS
        if active
        else SimpleNamespace(**(METRICS.__dict__ | {"has_activity": False})),
        counts={"total": total, "queued": 0, "failed": 0},
        activity={"points": []},
        ranges=(TimeRange.DAY, TimeRange.WEEK),
        events_configured=True,
        metrics_url="/domains/dom_1",
        metrics_partial_url="/domains/dom_1/partials/metrics",
    )


def test_every_tab_is_a_link_to_the_same_domain() -> None:
    page = _overview(_identity())

    for path in (
        "/domains/dom_1",
        "/domains/dom_1/emails",
        "/domains/dom_1/inbox",
        "/domains/dom_1/setup",
    ):
        assert f'href="{path}"' in page
    assert "<h1>alpha.example</h1>" in page


def test_an_address_is_not_offered_an_inbox() -> None:
    page = _overview(_identity(identity_type="email_address", value="me@example.com"))

    assert 'href="/domains/dom_1/inbox"' not in page
    assert 'href="/domains/dom_1/setup"' in page


def test_the_range_control_links_stay_on_the_domain() -> None:
    page = _overview(_identity())

    assert 'href="/domains/dom_1?range=7d"' in page
    assert 'hx-get="/domains/dom_1/partials/metrics?range=7d"' in page
    assert 'href="/?range=' not in page


def test_a_domain_with_no_mail_gets_an_empty_state_not_zeroes() -> None:
    page = _overview(_identity(), active=False, total=0)

    assert "Nothing sent from alpha.example" in page
    assert 'class="grid-metrics"' not in page


def test_the_emails_filter_stays_on_the_domain() -> None:
    page = _render(
        "pages/domain_emails.html",
        _identity(),
        "emails",
        emails=[],
        counts={"total": 0},
        statuses=(None, EmailStatus.SENT),
        selected_status=None,
        emails_url="/domains/dom_1/emails",
        emails_partial_url="/domains/dom_1/partials/emails",
    )

    assert 'href="/domains/dom_1/emails?status=sent"' in page
    assert 'href="/emails?status=' not in page


def _inbox(*, receiving_on: bool) -> str:
    return _render(
        "pages/domain_inbox.html",
        _identity(),
        "inbox",
        messages=[],
        has_more=False,
        older=None,
        domains=["alpha.example"],
        selected_domain=None,
        receiving_on=receiving_on,
        inbox_url="/domains/dom_1/inbox",
    )


def test_a_domain_that_is_not_receiving_is_pointed_at_setup() -> None:
    page = _inbox(receiving_on=False)

    assert "Not receiving mail for alpha.example" in page
    assert 'href="/domains/dom_1/setup"' in page


def test_a_receiving_domain_with_no_mail_is_waiting_not_filtered() -> None:
    page = _inbox(receiving_on=True)

    assert "Waiting for the first message" in page
    assert 'aria-label="Filter by domain"' not in page


def test_the_setup_tab_does_not_link_the_card_to_itself() -> None:
    page = _render(
        "pages/domain_setup.html",
        _identity(),
        "setup",
        connection=None,
        receiving={},
        workspace=True,
    )

    assert "Verification" in page
    assert 'action="/domains/dom_1/refresh"' in page
    assert ">Open<" not in page


def test_the_domains_list_card_links_to_the_domain() -> None:
    card = templates.env.get_template("partials/identity_row.html").render(
        identity=_identity(), csrf_token="token"
    )

    assert 'href="/domains/dom_1"' in card
    assert "Open" in card


def _card(**context: object) -> str:
    return templates.env.get_template("partials/identity_row.html").render(
        identity=_identity(), csrf_token="token", **context
    )


def test_the_card_says_how_much_was_sent_in_the_last_day() -> None:
    assert "42 sent" in _card(sent_24h={"dom_1": 42})


def test_the_card_says_when_nothing_was_sent() -> None:
    card = _card(sent_24h={"dom_1": 0})

    assert "Nothing sent" in card
    assert "0 sent" not in card


def test_a_card_without_the_counts_has_no_such_row() -> None:
    assert "Last 24 hours" not in _card()
