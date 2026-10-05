"""``GET /v1/inbound`` and ``GET /v1/inbound/{id}`` (inbound email, Phase C).

The read side of receiving. As with ``/v1/emails``, paging is where a list
endpoint goes wrong quietly - a wrong page returns real rows in a plausible order
and leaves some out - so a good part of this file is about the boundary: that a
cursor from another project cannot position a page, and that an id belonging to
somebody else is a 404 and never a 403, which would confirm it exists.

The other thing worth asserting is what is *not* in a response. The storage
bucket and key are internal and never leave; the summary carries no body.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from seskit_core.models import InboundEmail
from seskit_core.services import create_api_key, create_project, register_user
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

PASSWORD = "correct-horse-battery"
URL = "/v1/inbound"
FIRST_SECOND = 1_700_000_000
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _project_key(
    session: AsyncSession, *, owner: str, name: str = "Inbox"
) -> tuple[str, str]:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=name)
    issued = await create_api_key(session, project_id=project.id, name="prod")
    return project.id, issued.raw_key


async def _message(
    session: AsyncSession,
    *,
    project_id: str,
    second: int = 0,
    domain: str = "example.com",
    subject: str = "Hello there",
    **overrides: Any,
) -> InboundEmail:
    fields: dict[str, Any] = {
        "id": f"inbound_{ULID.from_timestamp(FIRST_SECOND + second)}",
        "project_id": project_id,
        "domain": domain,
        "provider_message_id": f"ses-{second}",
        "provider_event_id": f"sns-{second}-{project_id}",
        "storage_bucket": "seskit-inbound-123456789012-us-east-1",
        "storage_key": f"seskit-abc/{second}-{project_id}",
        "received_at": NOW,
        "raw_expires_at": NOW + timedelta(days=36500),
        "from_address": "ada@other.org",
        "from_name": "Ada Lovelace",
        "to_addresses": ["support@example.com"],
        "subject": subject,
        "text_body": "Hi.",
        "html_body": "<p>Hi.</p>",
        "spf_verdict": "PASS",
        "dkim_verdict": "GRAY",
    }
    row = InboundEmail(**(fields | overrides))
    session.add(row)
    await session.flush()
    return row


# ------------------------------------------------------------------- auth ---


@pytest.mark.parametrize("path", [URL, f"{URL}/inbound_x"])
async def test_a_request_without_a_key_is_refused(app_client: AsyncClient, path: str) -> None:
    response = await app_client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_failed"


# ------------------------------------------------------------------- list ---


async def test_an_empty_project_lists_nothing_rather_than_failing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, key = await _project_key(db_session, owner="a@example.com")
    await db_session.commit()

    response = await app_client.get(URL, headers=_auth(key))

    assert response.status_code == 200
    assert response.json() == {"data": [], "has_more": False}


async def test_the_list_describes_each_message_and_carries_no_body(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    row = await _message(db_session, project_id=project_id)
    await db_session.commit()

    (item,) = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    assert item["id"] == row.id
    assert item["from"] == "ada@other.org"
    assert item["to"] == ["support@example.com"]
    assert item["subject"] == "Hello there"
    assert item["domain"] == "example.com"
    assert item["verdicts"]["spf"] == "PASS"
    assert item["verdicts"]["dkim"] == "GRAY"
    # SES did not say, which is null and not a pass.
    assert item["verdicts"]["virus"] is None
    assert item["attachment_count"] == 0
    # A page of a hundred messages with their HTML in it is not what a list is for.
    for absent in ("text", "html", "headers", "attachments"):
        assert absent not in item


async def test_nothing_internal_ever_appears_in_a_response(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The bucket and the key say where a customer's mail is stored. Neither is
    the caller's to know, and neither is any use to them.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    row = await _message(db_session, project_id=project_id)
    await db_session.commit()

    listed = (await app_client.get(URL, headers=_auth(key))).text
    detail = (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).text

    for body in (listed, detail):
        assert "seskit-inbound-123456789012" not in body
        assert "storage_key" not in body
        assert "storage_bucket" not in body
        assert row.storage_key not in body
        assert "project_id" not in body


async def test_another_projects_mail_is_never_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    mine, key = await _project_key(db_session, owner="a@example.com")
    theirs, _ = await _project_key(db_session, owner="b@example.com")
    await _message(db_session, project_id=mine, subject="mine")
    await _message(db_session, project_id=theirs, subject="theirs", second=1)
    await db_session.commit()

    subjects = [
        m["subject"] for m in (await app_client.get(URL, headers=_auth(key))).json()["data"]
    ]

    assert subjects == ["mine"]


async def test_the_list_is_newest_first_and_pages_by_cursor(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    ids = [(await _message(db_session, project_id=project_id, second=n)).id for n in range(5)]
    await db_session.commit()
    newest_first = list(reversed(ids))

    first = (await app_client.get(URL, params={"limit": 2}, headers=_auth(key))).json()
    second = (
        await app_client.get(
            URL,
            params={"limit": 2, "starting_after": first["data"][-1]["id"]},
            headers=_auth(key),
        )
    ).json()
    third = (
        await app_client.get(
            URL,
            params={"limit": 2, "starting_after": second["data"][-1]["id"]},
            headers=_auth(key),
        )
    ).json()

    assert [m["id"] for m in first["data"]] == newest_first[:2]
    assert first["has_more"] is True
    assert [m["id"] for m in second["data"]] == newest_first[2:4]
    assert second["has_more"] is True
    assert [m["id"] for m in third["data"]] == newest_first[4:]
    # The last page says so, and costs no request that comes back empty.
    assert third["has_more"] is False


async def test_a_page_is_stable_while_mail_arrives_underneath_it(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reason for a cursor over an offset. With an offset, a message arriving
    between two pages shifts every row down one and the reader silently skips
    the one that moved across the boundary.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    for n in range(4):
        await _message(db_session, project_id=project_id, second=n)
    await db_session.commit()
    first = (await app_client.get(URL, params={"limit": 2}, headers=_auth(key))).json()

    await _message(db_session, project_id=project_id, second=100)
    await db_session.commit()

    second = (
        await app_client.get(
            URL,
            params={"limit": 2, "starting_after": first["data"][-1]["id"]},
            headers=_auth(key),
        )
    ).json()
    seen = [m["id"] for m in first["data"]] + [m["id"] for m in second["data"]]
    assert len(seen) == len(set(seen)) == 4


async def test_a_cursor_from_another_project_is_a_404_not_a_page(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The comparison is lexical, so a foreign id would otherwise position a page
    of real messages by a value the caller cannot see - a wrong answer that
    looks like a right one.
    """
    mine, key = await _project_key(db_session, owner="a@example.com")
    theirs, _ = await _project_key(db_session, owner="b@example.com")
    await _message(db_session, project_id=mine)
    foreign = await _message(db_session, project_id=theirs, second=50)
    await db_session.commit()

    response = await app_client.get(URL, params={"starting_after": foreign.id}, headers=_auth(key))

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_an_unknown_cursor_is_a_404_not_an_empty_page(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, key = await _project_key(db_session, owner="a@example.com")
    await db_session.commit()

    response = await app_client.get(
        URL, params={"starting_after": "inbound_nope"}, headers=_auth(key)
    )

    assert response.status_code == 404


async def test_the_domain_filter_narrows_the_list_and_ignores_case(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _message(db_session, project_id=project_id, domain="example.com", second=1)
    await _message(db_session, project_id=project_id, domain="example.net", second=2)
    await db_session.commit()

    response = await app_client.get(URL, params={"domain": " Example.NET "}, headers=_auth(key))

    assert [m["domain"] for m in response.json()["data"]] == ["example.net"]


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_a_page_size_out_of_range_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, limit: int
) -> None:
    _, key = await _project_key(db_session, owner="a@example.com")
    await db_session.commit()

    response = await app_client.get(URL, params={"limit": limit}, headers=_auth(key))

    assert response.status_code in (400, 422)
    assert "error" in response.json()


# ----------------------------------------------------------------- detail ---


async def test_one_message_comes_back_in_full(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    row = await _message(
        db_session,
        project_id=project_id,
        envelope_from="bounce@other.org",
        envelope_to=["support@example.com", "bcc@example.com"],
        reply_to=["replies@other.org"],
        message_id_header="abc@other.org",
        in_reply_to="first@example.com",
        reference_ids=["root@example.com", "first@example.com"],
        attachments=[
            {
                "index": 0,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "size": 9,
                "inline": False,
                "content_id": None,
            }
        ],
        headers=[["Subject", "Hello there"], ["X-Odd", "1"]],
        size_bytes=1234,
    )
    await db_session.commit()

    body = (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).json()

    assert body["id"] == row.id
    assert body["text"] == "Hi."
    assert body["html"] == "<p>Hi.</p>"
    assert body["envelope_from"] == "bounce@other.org"
    # A blind copy is in the envelope list and in neither header list.
    assert body["recipients"] == ["support@example.com", "bcc@example.com"]
    assert body["to"] == ["support@example.com"]
    assert body["message_id"] == "abc@other.org"
    assert body["in_reply_to"] == "first@example.com"
    assert body["references"] == ["root@example.com", "first@example.com"]
    assert body["reply_to"] == ["replies@other.org"]
    assert body["attachments"] == [
        {
            "index": 0,
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size": 9,
            "inline": False,
            "content_id": None,
        }
    ]
    assert body["attachment_count"] == 1
    assert body["headers"] == [
        {"name": "Subject", "value": "Hello there"},
        {"name": "X-Odd", "value": "1"},
    ]
    assert body["size"] == 1234


async def test_hostile_content_is_returned_exactly_as_sent(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The API describes what arrived; it does not clean it. A consumer that
    renders it has to treat it as hostile, and the field says so.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    hostile = '<script>alert(1)</script><img src="http://tracker.example/p.gif">'
    row = await _message(db_session, project_id=project_id, html_body=hostile)
    await db_session.commit()

    body = (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).json()

    assert body["html"] == hostile


async def test_another_projects_message_is_a_404_never_a_403(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A 403 would confirm the id exists. It must look exactly like one that
    never did.
    """
    _, key = await _project_key(db_session, owner="a@example.com")
    theirs, _ = await _project_key(db_session, owner="b@example.com")
    foreign = await _message(db_session, project_id=theirs)
    await db_session.commit()

    foreign_response = await app_client.get(f"{URL}/{foreign.id}", headers=_auth(key))
    unknown_response = await app_client.get(f"{URL}/inbound_01NEVEREXISTED", headers=_auth(key))

    assert foreign_response.status_code == 404
    assert foreign_response.json() == unknown_response.json()


async def test_an_unreadable_message_is_still_a_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    row = await _message(
        db_session,
        project_id=project_id,
        parse_failed=True,
        text_body="",
        html_body="",
        from_address="",
        subject="",
    )
    await db_session.commit()

    body = (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).json()

    assert body["parse_failed"] is True
    assert body["text"] == ""
    assert body["attachments"] == []


# -------------------------------------------------- the original's lifetime ---


async def test_the_original_is_available_until_retention_passes(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    live = await _message(db_session, project_id=project_id, second=1)
    expired = await _message(
        db_session,
        project_id=project_id,
        second=2,
        raw_expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    unknown = await _message(db_session, project_id=project_id, second=3, raw_expires_at=None)
    await db_session.commit()

    async def available(row: InboundEmail) -> bool:
        body = (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).json()
        return bool(body["raw_available"])

    assert await available(live) is True
    assert await available(expired) is False
    # Unknown is not "gone": the answer is "may still be there", and the download
    # settles it.
    assert await available(unknown) is True


async def test_the_parsed_message_is_still_readable_after_the_original_expires(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The row outlives the bucket on purpose: it costs almost nothing and the
    files cost storage.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    row = await _message(
        db_session, project_id=project_id, raw_expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    await db_session.commit()

    response = await app_client.get(f"{URL}/{row.id}", headers=_auth(key))

    assert response.status_code == 200
    assert response.json()["text"] == "Hi."
    assert response.json()["raw_available"] is False


async def test_a_response_carries_the_rate_limit_headers(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, key = await _project_key(db_session, owner="a@example.com")
    await db_session.commit()

    response = await app_client.get(URL, headers=_auth(key))

    assert response.headers["X-RateLimit-Limit"]
    assert response.headers["X-RateLimit-Remaining"]
    assert response.headers["X-RateLimit-Reset"]
