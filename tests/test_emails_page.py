"""The Emails pages.

Mailpit shows what left the building; these show what SESKit recorded - which is
the only place a *failed* send is visible, since Mailpit by definition never saw
one.

The XSS test is the one that matters. The HTML body is chosen by whoever holds
an API key, and rendering it into the account owner's authenticated session
would be stored XSS with a session cookie attached.
"""

from __future__ import annotations

import base64
from typing import Any

from httpx import AsyncClient
from seskit_core.services import create_api_key, create_project
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
EMAILS_URL = "/v1/emails"

BODY: dict[str, Any] = {
    "from": "hello@example.com",
    "to": ["user@example.com"],
    "subject": "Welcome to Acme",
    "html": "<h1>Welcome!</h1>",
    "text": "Welcome",
}


async def _sign_in(client: AsyncClient, email: str = "owner@example.com") -> None:
    response = await client.post(
        "/signup", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


async def _key_for_signed_in_project(client: AsyncClient, session: AsyncSession) -> str:
    """An API key for the project the signed-in user is looking at."""
    from seskit_core.models import Project
    from sqlalchemy import select

    project = await session.scalar(select(Project))
    assert project is not None
    issued = await create_api_key(session, project_id=project.id, name="page")
    await session.commit()
    return issued.raw_key


async def _send(client: AsyncClient, raw_key: str, **overrides: Any) -> str:
    response = await client.post(
        EMAILS_URL, json={**BODY, **overrides}, headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# ------------------------------------------------------------------- page ---


async def test_the_page_needs_a_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/emails", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_an_empty_project_is_told_how_to_send(app_client: AsyncClient) -> None:
    """A new install should meet an instruction, not a blank table."""
    await _sign_in(app_client)

    page = await app_client.get("/emails")

    assert page.status_code == 200
    assert "Nothing sent yet" in page.text
    assert "/v1/emails" in page.text


async def test_a_sent_message_is_listed(app_client: AsyncClient, db_session: AsyncSession) -> None:
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)

    page = await app_client.get("/emails")

    assert "Welcome to Acme" in page.text
    assert "user@example.com" in page.text


async def test_a_blind_copy_is_not_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Recorded, but a blind copy shown beside the other recipients is not
    blind.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key, bcc=["quiet@example.com"])

    page = await app_client.get("/emails")

    assert "quiet@example.com" not in page.text


async def test_the_id_is_listed_and_links_to_the_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """§17 names an ID column. It is the link, so the row does not carry two
    links to the same page - with a hidden word for screen readers, because a
    link list full of bare ULIDs says nothing about where any of them go.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    email_id = await _send(app_client, raw_key)

    page = await app_client.get("/emails")

    assert email_id in page.text
    assert f'href="/emails/{email_id}"' in page.text
    assert "visually-hidden" in page.text


# ----------------------------------------------------------------- filter ---


async def _send_failed(session: AsyncSession, project_id: str) -> str:
    """A failed message, which the API cannot be asked to produce on demand."""
    from seskit_core.models import Email, EmailStatus

    email = Email(
        project_id=project_id,
        from_address="hello@example.com",
        to_addresses=["nobody@example.com"],
        subject="Undeliverable notice",
        status=EmailStatus.FAILED.value,
        last_error="Rejected by the provider",
    )
    session.add(email)
    await session.commit()
    return email.id


async def _project_id(session: AsyncSession) -> str:
    from seskit_core.models import Project
    from sqlalchemy import select

    project_id = await session.scalar(select(Project.id))
    assert project_id is not None
    return str(project_id)


async def test_a_filter_narrows_the_list(app_client: AsyncClient, db_session: AsyncSession) -> None:
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)
    await _send_failed(db_session, await _project_id(db_session))

    page = await app_client.get("/emails?status=failed")

    assert "Undeliverable notice" in page.text
    assert "Welcome to Acme" not in page.text


async def test_an_unknown_status_shows_everything_rather_than_failing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A hand-edited or stale URL should render the page it was plainly asking
    for. There is nothing to protect: the value only narrows a query already
    scoped to one project.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)

    page = await app_client.get("/emails?status=not-a-status")

    assert page.status_code == 200
    assert "Welcome to Acme" in page.text


async def test_the_totals_do_not_move_when_the_list_is_filtered(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """They are the project's totals. A "Total" that changed when you clicked
    "Failed" would no longer mean anything.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)
    await _send_failed(db_session, await _project_id(db_session))

    unfiltered = await app_client.get("/emails")
    filtered = await app_client.get("/emails?status=failed")

    for page in (unfiltered, filtered):
        assert page.text.count(">Total<") == 1
    assert _metric_after(unfiltered.text, "Total") == _metric_after(filtered.text, "Total")


def _metric_after(html: str, label: str) -> str:
    """The value rendered in the metric tile carrying ``label``.

    The macro puts the label before the value, so this reads forwards from the
    label and takes the first value it meets.
    """
    import re

    rest = html[html.index(f">{label}<") :]
    found = re.search(r'class="metric__value"[^>]*>\s*([^<]+)', rest)
    return found.group(1).strip() if found else ""


async def test_an_empty_filter_result_is_not_the_onboarding_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A project with sent mail and no failures is not a new install, and
    telling it to create its first API key would be nonsense.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)

    page = await app_client.get("/emails?status=failed")

    assert "No failed messages" in page.text
    assert "Nothing sent yet" not in page.text


async def test_the_fragment_returns_the_table_alone(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """What the filter swaps over HTMX, so it must not carry the whole page."""
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)

    fragment = await app_client.get("/partials/emails")

    assert fragment.status_code == 200
    assert "Welcome to Acme" in fragment.text
    assert "<html" not in fragment.text


async def test_the_fragment_needs_a_session(app_client: AsyncClient) -> None:
    """A project's mail, authenticated like the page it belongs to."""
    response = await app_client.get("/partials/emails", follow_redirects=False)

    assert response.status_code == 303


# ----------------------------------------------------------------- detail ---


async def test_the_detail_view_shows_the_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    email_id = await _send(app_client, raw_key)

    page = await app_client.get(f"/emails/{email_id}")

    assert page.status_code == 200
    assert "Welcome to Acme" in page.text
    assert "hello@example.com" in page.text


async def test_the_html_body_is_shown_as_source_not_rendered(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The test that matters.

    The body is chosen by whoever holds an API key. Rendering it here would
    execute their markup inside the account owner's authenticated session.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    payload = '<script>alert("xss")</script>'
    email_id = await _send(app_client, raw_key, html=payload)

    page = await app_client.get(f"/emails/{email_id}")

    assert payload not in page.text
    assert "&lt;script&gt;" in page.text


async def test_an_attachment_is_listed_without_its_content(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    content = base64.b64encode(b"secret,values\n").decode()
    email_id = await _send(
        app_client,
        raw_key,
        attachments=[{"filename": "report.csv", "content": content, "content_type": "text/csv"}],
    )

    page = await app_client.get(f"/emails/{email_id}")

    assert "report.csv" in page.text
    assert "secret,values" not in page.text


async def test_an_email_from_another_project_is_not_reachable(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ownership is part of the query, so an id outside the selected project
    resolves to nothing - the same answer as "no such email", so a stranger
    cannot probe for real ids.

    A second project belonging to the *same* user, because Phase 2 already
    refuses a switch to someone else's - that guard would mask this one.
    """
    from seskit_core.models import User
    from sqlalchemy import select

    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    email_id = await _send(app_client, raw_key)

    owner = await db_session.scalar(select(User))
    assert owner is not None
    second = await create_project(db_session, user_id=owner.id, name="Second")
    await db_session.commit()

    switched = await app_client.post(
        "/projects/switch",
        data={"project_id": second.id, "csrf_token": await _csrf(app_client)},
        follow_redirects=False,
    )
    assert switched.status_code == 303

    page = await app_client.get(f"/emails/{email_id}", follow_redirects=False)

    assert page.status_code == 303
    assert "Welcome to Acme" not in (await app_client.get("/emails")).text


async def _csrf(client: AsyncClient) -> str:
    page = await client.get("/emails")
    marker = 'name="csrf_token" value="'
    if marker not in page.text:
        return ""
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# --------------------------------------------------------------- overview ---


async def test_the_overview_reports_real_counts(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It showed hardcoded zeroes from Phase 1 until there was something to
    count.
    """
    await _sign_in(app_client)
    raw_key = await _key_for_signed_in_project(app_client, db_session)
    await _send(app_client, raw_key)

    page = await app_client.get("/")

    assert "1 message recorded" in page.text


# ----------------------------------------------------- send a test message ---

# The dashboard's one send control. It exists because the checklist's second
# step says "Send a test message" and this page used to answer with a curl
# command - homework rather than an answer to "does mail actually leave this
# thing", which is the first question anybody has after connecting AWS.
#
# What is worth testing is that it is not a special case. It calls
# `accept_email`, the same service `POST /v1/emails` calls, so a message sent
# from here has to meet every refusal an application would meet. A form with
# its own quieter rules would prove nothing about the product.


async def _csrf_from(client: AsyncClient, path: str) -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _verified_sender(session: AsyncSession, value: str = "otito.site") -> str:
    """A connected project with one verified domain, which is what the form
    needs before it will offer anything.
    """
    from fakes.ses import ACCOUNT_ID, FAKE_CREDENTIALS, TEST_SECRET_KEY
    from seskit_core.models import AWSConnection, ConnectionStatus, Identity, Project
    from seskit_core.providers.types import IdentityType, VerificationStatus
    from seskit_core.security.aws_credentials import encrypt_secret_access_key
    from sqlalchemy import select

    project = await session.scalar(select(Project))
    assert project is not None

    session.add(
        AWSConnection(
            project_id=project.id,
            region="eu-west-2",
            aws_account_id=ACCOUNT_ID,
            status=ConnectionStatus.CONNECTED.value,
            aws_access_key_id=FAKE_CREDENTIALS.access_key_id,
            aws_secret_access_key_encrypted=encrypt_secret_access_key(
                FAKE_CREDENTIALS.secret_access_key, secret_key=TEST_SECRET_KEY
            ),
        )
    )
    session.add(
        Identity(
            project_id=project.id,
            value=value,
            identity_type=IdentityType.DOMAIN.value,
            region="eu-west-2",
            verification_status=VerificationStatus.SUCCESS.value,
            dkim_tokens=[],
        )
    )
    await session.commit()
    return str(project.id)


async def test_the_form_offers_only_verified_senders(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unverified sender becomes impossible rather than an error afterwards.

    SES refuses one, so a form that let you pick it would be a form whose main
    job is producing a failure you could have been spared.
    """
    await _sign_in(app_client)
    await _verified_sender(db_session)
    await _verified_sender(db_session, value="unverified.example")

    page = await app_client.get("/emails")

    assert 'id="sender"' in page.text
    assert "hello@otito.site" in page.text


async def test_with_nothing_verified_the_card_says_so(app_client: AsyncClient) -> None:
    """Rather than a form that can only fail. There is nothing it could
    honestly offer, so it points at the page that fixes that.
    """
    await _sign_in(app_client)

    page = await app_client.get("/emails")

    assert "No verified sender yet" in page.text
    assert 'action="/emails/test"' not in page.text


async def test_a_test_message_is_queued_and_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    await _verified_sender(db_session)

    response = await app_client.post(
        "/emails/test",
        data={
            "csrf_token": await _csrf_from(app_client, "/emails"),
            "sender": "hello@otito.site",
            "to": "someone@example.com",
            "subject": "A test from SESKit",
            "body": "If you are reading this, SESKit sent it.",
        },
    )

    assert response.status_code == 200
    assert "Queued for someone@example.com" in response.text
    # On the page it just rendered, not only in the database.
    assert "A test from SESKit" in response.text


async def test_an_unverified_sender_is_refused_even_if_asked_for_directly(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The select offers only verified senders; that is a convenience, not the
    check. A hand-made POST has to meet the same refusal the API would give,
    because `accept_email` is what decides and not the form.
    """
    await _sign_in(app_client)
    await _verified_sender(db_session)

    response = await app_client.post(
        "/emails/test",
        data={
            "csrf_token": await _csrf_from(app_client, "/emails"),
            "sender": "hello@somewhere-else.example",
            "to": "someone@example.com",
            "subject": "Nope",
            "body": "Nope",
        },
    )

    assert response.status_code == 400
    assert "not a verified sender" in response.text


async def test_a_suppressed_recipient_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Same list, same refusal, same wording as the API gives. A dashboard that
    could send to a suppressed address would be a way around the suppression
    list, which is the one thing it must not be.
    """
    from seskit_core.models import SuppressionReason
    from seskit_core.services import suppress

    await _sign_in(app_client)
    project_id = await _verified_sender(db_session)
    await suppress(
        db_session,
        project_id=project_id,
        address="blocked@example.com",
        reason=SuppressionReason.BOUNCE,
    )
    await db_session.commit()

    response = await app_client.post(
        "/emails/test",
        data={
            "csrf_token": await _csrf_from(app_client, "/emails"),
            "sender": "hello@otito.site",
            "to": "blocked@example.com",
            "subject": "Nope",
            "body": "Nope",
        },
    )

    assert response.status_code == 400
    assert "suppression list" in response.text


async def test_the_send_form_needs_a_csrf_token(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It sends mail from a signed-in session, so a cross-site POST that could
    reach it is a way to send from somebody else's instance.
    """
    await _sign_in(app_client)
    await _verified_sender(db_session)

    response = await app_client.post(
        "/emails/test",
        data={
            "sender": "hello@otito.site",
            "to": "someone@example.com",
            "subject": "No token",
            "body": "No token",
        },
    )

    assert response.status_code == 403
