"""``GET /v1/emails`` (§11, §13, §31 Phase 12).

The endpoint `EmailList` was declared for and nothing returned. `emails.list()`
is one of the three calls §13 asks the SDK for, and a client cannot wrap an
endpoint that does not exist.

**Paging is where a list endpoint goes wrong.** A wrong page does not raise; it
returns real rows in a plausible order and quietly leaves some out. So most of
this file is about the boundary: that a page is stable while messages are being
inserted, that a cursor from another project cannot position a page, and that
`has_more` means what it says.
"""

from __future__ import annotations

from httpx import AsyncClient
from seskit_core.models import Email, EmailStatus
from seskit_core.services import create_api_key, create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

PASSWORD = "correct-horse-battery"
EMAILS_URL = "/v1/emails"

#: A second apart, so ordering is decided by the timestamp a ULID opens with
#: rather than by the random bits that break a tie inside one millisecond.
#: Rows written in a loop land in the same millisecond, and the real ordering
#: guarantee is only as fine as the clock.
FIRST_SECOND = 1_700_000_000


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _project_key(session: AsyncSession, *, owner: str, name: str) -> tuple[str, str]:
    """A project and an API key for it. Returns ``(project_id, raw_key)``."""
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=name)
    issued = await create_api_key(session, project_id=project.id, name="prod")
    return project.id, issued.raw_key


async def _send(
    session: AsyncSession,
    *,
    project_id: str,
    subject: str = "Welcome",
    status: str = EmailStatus.SENT.value,
    second: int = 0,
) -> Email:
    row = Email(
        id=f"email_{ULID.from_timestamp(FIRST_SECOND + second)}",
        project_id=project_id,
        from_address="hello@example.com",
        to_addresses=["user@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject=subject,
        text_body="Hello",
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------- reading ---


async def test_an_empty_project_lists_nothing_rather_than_failing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")

    response = await app_client.get(EMAILS_URL, headers=_auth(raw_key))

    assert response.status_code == 200
    assert response.json() == {"data": [], "has_more": False}


async def test_a_sent_message_is_listed(app_client: AsyncClient, db_session: AsyncSession) -> None:
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    email = await _send(db_session, project_id=project_id)
    await db_session.commit()

    body = (await app_client.get(EMAILS_URL, headers=_auth(raw_key))).json()

    assert [row["id"] for row in body["data"]] == [email.id]
    assert body["data"][0]["subject"] == "Welcome"


async def test_the_newest_message_comes_first(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """What a send log is read for. Oldest-first would put the message somebody
    is looking for on the last page.
    """
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    first = await _send(db_session, project_id=project_id, subject="First", second=0)
    second = await _send(db_session, project_id=project_id, subject="Second", second=1)
    await db_session.commit()

    body = (await app_client.get(EMAILS_URL, headers=_auth(raw_key))).json()

    assert [row["id"] for row in body["data"]] == [second.id, first.id]


async def test_bcc_is_still_not_returned(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """A blind copy is blind wherever the message is read from. The detail
    endpoint withholds it; a list that did not would be a way around that.
    """
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    row = await _send(db_session, project_id=project_id)
    row.bcc_addresses = ["archive@example.com"]
    await db_session.commit()

    body = (await app_client.get(EMAILS_URL, headers=_auth(raw_key))).json()

    assert "bcc" not in body["data"][0]
    assert "archive@example.com" not in str(body)


async def test_the_custom_headers_come_back_in_a_list_too(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The list returns the same shape as the detail endpoint. A field added to
    one and not the other is how two representations of one row drift.
    """
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    row = await _send(db_session, project_id=project_id)
    row.headers = {"X-Entity-Ref-Id": "order-1234"}
    await db_session.commit()

    body = (await app_client.get(EMAILS_URL, headers=_auth(raw_key))).json()

    assert body["data"][0]["headers"] == {"X-Entity-Ref-Id": "order-1234"}


# ----------------------------------------------------------------- paging ---


async def test_a_page_stops_at_the_limit_and_says_there_is_more(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    for index in range(3):
        await _send(db_session, project_id=project_id, subject=f"Message {index}", second=index)
    await db_session.commit()

    body = (await app_client.get(f"{EMAILS_URL}?limit=2", headers=_auth(raw_key))).json()

    assert len(body["data"]) == 2
    assert body["has_more"] is True


async def test_the_last_page_says_there_is_no_more(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`has_more` exists so a caller can stop without a final request that comes
    back empty.
    """
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    for index in range(2):
        await _send(db_session, project_id=project_id, subject=f"Message {index}", second=index)
    await db_session.commit()

    body = (await app_client.get(f"{EMAILS_URL}?limit=2", headers=_auth(raw_key))).json()

    assert len(body["data"]) == 2
    assert body["has_more"] is False


async def test_the_cursor_continues_where_the_page_stopped(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    sent = [
        await _send(db_session, project_id=project_id, subject=f"Message {index}", second=index)
        for index in range(3)
    ]
    await db_session.commit()
    newest_first = [row.id for row in reversed(sent)]

    first_page = (await app_client.get(f"{EMAILS_URL}?limit=2", headers=_auth(raw_key))).json()
    cursor = first_page["data"][-1]["id"]
    second_page = (
        await app_client.get(
            f"{EMAILS_URL}?limit=2&starting_after={cursor}", headers=_auth(raw_key)
        )
    ).json()

    assert [row["id"] for row in first_page["data"]] == newest_first[:2]
    assert [row["id"] for row in second_page["data"]] == newest_first[2:]
    assert second_page["has_more"] is False


async def test_a_message_sent_between_pages_does_not_hide_another(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reason this pages by cursor rather than by offset.

    With `offset=2` the new message shifts every row down one, and the message
    that was on the boundary is never returned at all. A cursor names a fixed
    point, so the second page picks up exactly where the first stopped.
    """
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    sent = [
        await _send(db_session, project_id=project_id, subject=f"Message {index}", second=index)
        for index in range(3)
    ]
    await db_session.commit()

    first_page = (await app_client.get(f"{EMAILS_URL}?limit=2", headers=_auth(raw_key))).json()
    await _send(db_session, project_id=project_id, subject="Arrived mid-read", second=9)
    await db_session.commit()

    cursor = first_page["data"][-1]["id"]
    second_page = (
        await app_client.get(
            f"{EMAILS_URL}?limit=2&starting_after={cursor}", headers=_auth(raw_key)
        )
    ).json()

    assert [row["id"] for row in second_page["data"]] == [sent[0].id]


async def test_a_cursor_that_is_not_ours_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ids sort lexically, so another project's id would happily position a page
    of our own messages - a wrong answer that looks like a right one. It is a
    404 for the same reason the detail endpoint is: confirming the id exists
    would say something about a project this key cannot see.
    """
    mine, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    theirs, _ = await _project_key(db_session, owner="other@example.com", name="Theirs")
    await _send(db_session, project_id=mine)
    stranger = await _send(db_session, project_id=theirs)
    await db_session.commit()

    response = await app_client.get(
        f"{EMAILS_URL}?starting_after={stranger.id}", headers=_auth(raw_key)
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_a_page_larger_than_the_cap_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A cap rather than a suggestion: without one a project with a year of
    sends can ask for all of it in one query.
    """
    _, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")

    response = await app_client.get(f"{EMAILS_URL}?limit=500", headers=_auth(raw_key))

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request"


# --------------------------------------------------------------- filtering ---


async def test_the_status_filter_narrows_the_list(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    await _send(db_session, project_id=project_id, status=EmailStatus.SENT.value, second=0)
    failed = await _send(
        db_session, project_id=project_id, status=EmailStatus.FAILED.value, second=1
    )
    await db_session.commit()

    body = (await app_client.get(f"{EMAILS_URL}?status=failed", headers=_auth(raw_key))).json()

    assert [row["id"] for row in body["data"]] == [failed.id]


async def test_a_status_that_does_not_exist_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")

    response = await app_client.get(f"{EMAILS_URL}?status=pending", headers=_auth(raw_key))

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request"


# ----------------------------------------------------------------- tenancy ---


async def test_another_project_s_messages_are_not_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The guarantee every /v1 route makes. A list endpoint is where a missing
    project filter shows up as somebody else's mail on your screen.
    """
    mine, raw_key = await _project_key(db_session, owner="owner@example.com", name="Sending")
    theirs, _ = await _project_key(db_session, owner="other@example.com", name="Theirs")
    ours = await _send(db_session, project_id=mine, subject="Ours")
    await _send(db_session, project_id=theirs, subject="Theirs")
    await db_session.commit()

    body = (await app_client.get(EMAILS_URL, headers=_auth(raw_key))).json()

    assert [row["id"] for row in body["data"]] == [ours.id]


async def test_listing_needs_a_key(app_client: AsyncClient) -> None:
    response = await app_client.get(EMAILS_URL)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_failed"
