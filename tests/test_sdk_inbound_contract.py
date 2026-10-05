"""The SDK's inbound calls against the real API (inbound email, Phase C).

``tests/test_sdk_inbound_transport.py`` asserts the client sends what it thinks it
sends. This asserts that what it thinks is right: the same client, pointed at the
actual application, with the actual Postgres behind it. A stub written from the
author's memory of the API agrees with a misremembering for ever; here a wrong
field name is a missing attribute and a renamed error type is an exception nobody
can catch.

Like ``test_sdk_contract`` it uses the async client, because ``httpx.ASGITransport``
is async-only. The two clients share the builders, the URL, the headers and the
transport rule, so what is proved here holds for both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fakes.inbound import FakeInboundStore
from httpx import AsyncClient
from seskit import InboundEmail, InboundSummary, NotFound
from sqlalchemy.ext.asyncio import AsyncSession
from test_sdk_contract import _key, _sdk
from test_v1_inbound import _message
from test_v1_inbound_downloads import PDF, _raw, _stored


async def test_the_list_reads_back_what_the_api_sent(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _key(db_session)
    await _message(db_session, project_id=project_id, subject="Hello there")
    await db_session.commit()

    page = await _sdk(app_client, key).inbound.list()

    assert len(page) == 1
    (row,) = page.data
    assert isinstance(row, InboundSummary)
    assert not isinstance(row, InboundEmail)
    assert row.subject == "Hello there"
    assert row.from_ == "ada@other.org"
    assert row.to == ["support@example.com"]
    assert row.verdicts.spf == "PASS"
    assert row.verdicts.dkim == "GRAY"
    # Left null by the API, so None here - not a pass.
    assert row.verdicts.virus is None
    assert row.received_at is not None
    assert row.received_at.tzinfo is not None
    assert page.has_more is False


async def test_one_message_reads_back_in_full(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _key(db_session)
    row = await _message(
        db_session,
        project_id=project_id,
        envelope_to=["support@example.com", "bcc@example.com"],
        message_id_header="abc@other.org",
        reference_ids=["root@example.com"],
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
    )
    await db_session.commit()

    message = await _sdk(app_client, key).inbound.get(row.id)

    assert isinstance(message, InboundEmail)
    assert message.text == "Hi."
    assert message.html == "<p>Hi.</p>"
    assert message.recipients == ["support@example.com", "bcc@example.com"]
    assert message.message_id == "abc@other.org"
    assert message.references == ["root@example.com"]
    assert [a.filename for a in message.attachments] == ["report.pdf"]
    assert message.attachments[0].index == 0
    assert message.headers == [("Subject", "Hello there"), ("X-Odd", "1")]
    assert message.raw_available is True


async def test_paging_by_last_id_neither_skips_nor_repeats(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _key(db_session)
    for n in range(5):
        await _message(db_session, project_id=project_id, second=n, subject=f"m{n}")
    await db_session.commit()
    inbound = _sdk(app_client, key).inbound

    seen: list[str] = []
    page = await inbound.list(limit=2)
    seen += [row.subject for row in page]
    while page.has_more:
        page = await inbound.list(limit=2, starting_after=page.last_id)
        seen += [row.subject for row in page]

    assert seen == ["m4", "m3", "m2", "m1", "m0"]


async def test_the_domain_filter_reaches_the_api(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _key(db_session)
    await _message(db_session, project_id=project_id, domain="example.com", second=1)
    await _message(db_session, project_id=project_id, domain="example.net", second=2)
    await db_session.commit()

    page = await _sdk(app_client, key).inbound.list(domain="example.net")

    assert [row.domain for row in page] == ["example.net"]


async def test_another_projects_message_is_not_found(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, mine = await _key(db_session, owner="mine@example.com")
    theirs_project, _ = await _key(db_session, owner="theirs@example.com")
    foreign = await _message(db_session, project_id=theirs_project)
    await db_session.commit()

    with pytest.raises(NotFound):
        await _sdk(app_client, mine).inbound.get(foreign.id)


async def test_an_id_that_tries_to_reach_another_endpoint_is_just_not_found(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Against the real API, not a stub: ``../emails`` has to arrive as one
    segment that nothing matches, and not as a different endpoint's answer.
    """
    _, key = await _key(db_session)
    await db_session.commit()

    with pytest.raises(NotFound):
        await _sdk(app_client, key).inbound.get("../emails")


# ------------------------------------------------------------- downloads ---


async def test_an_attachment_and_the_original_come_back_byte_for_byte(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """The bytes survive a trip through the real API and the real transport: no
    decoding as text, no JSON, nothing between a PDF in a bucket and a PDF in the
    caller's hands.
    """
    _, key, row = await _stored(db_session, inbound_store, owner="owner@example.com")
    inbound = _sdk(app_client, key).inbound

    assert await inbound.attachment(row.id, 0) == PDF
    assert await inbound.raw(row.id) == _raw()


async def test_an_attachment_that_does_not_exist_is_not_found(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(db_session, inbound_store, owner="owner@example.com")

    with pytest.raises(NotFound):
        await _sdk(app_client, key).inbound.attachment(row.id, 9)


async def test_a_download_after_retention_is_not_found_but_the_message_still_reads(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(
        db_session,
        inbound_store,
        owner="owner@example.com",
        raw_expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    inbound = _sdk(app_client, key).inbound

    with pytest.raises(NotFound) as caught:
        await inbound.raw(row.id)

    assert "no longer in storage" in str(caught.value)
    message = await inbound.get(row.id)
    assert message.text == "Hi."
    assert message.raw_available is False
