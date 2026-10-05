"""Downloading an attachment, and the original message (inbound email, Phase C).

These routes serve bytes a stranger wrote, from the API's own origin. That is the
whole risk and most of this file is about it: whatever the sender declared, the
response has to be a download and not a document, and the headers that make it
one have to be present on every path.

The other thing worth asserting is what does *not* happen. A request that can be
refused without touching S3 - an index that does not exist, an expired message, a
project that is not connected - must be, because each fetch is a transfer of up to
40 MB, and one the caller could have been told about for free is a cost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes.inbound import EXPIRED, FakeInboundStore
from fakes.ses import FAKE_CREDENTIALS, connect_project
from httpx import AsyncClient
from seskit_api.routes.v1.inbound import DOWNLOAD_HEADERS, content_disposition
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import InboundEmail
from sqlalchemy.ext.asyncio import AsyncSession
from test_v1_inbound import _auth, _message, _project_key

URL = "/v1/inbound"
PDF = b"%PDF-1.4\n"
HTML_ATTACHMENT = b"<html><script>alert(document.cookie)</script></html>"


def _raw() -> bytes:
    """A message with a PDF and a hostile HTML file, as the wire carries it."""
    return (
        b"From: ada@other.org\r\nTo: support@example.com\r\nSubject: with files\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
        b"--M\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
        b"--M\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n"
        # Declares itself HTML and carries a path and a header-injection attempt in
        # its name, which can only reach a header through RFC 2231.
        b"--M\r\nContent-Type: text/html\r\n"
        b"Content-Disposition: attachment;"
        b" filename*=UTF-8''..%5C..%2Fevil%0D%0ASet-Cookie%3A%20a%3Db.html\r\n\r\n"
        + HTML_ATTACHMENT
        + b"\r\n--M--\r\n"
    )


ATTACHMENTS = [
    {
        "index": 0,
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": len(PDF),
        "inline": False,
        "content_id": None,
    },
    {
        "index": 1,
        "filename": "evil.html",
        "content_type": "text/html",
        "size": len(HTML_ATTACHMENT),
        "inline": False,
        "content_id": None,
    },
]


async def _stored(
    session: AsyncSession,
    store: FakeInboundStore,
    *,
    owner: str = "a@example.com",
    connected: bool = True,
    **overrides: Any,
) -> tuple[str, str, InboundEmail]:
    """A project with a key, a message in it, and the original in the store."""
    project_id, key = await _project_key(session, owner=owner)
    if connected:
        await connect_project(session, project_id)
    # Defaults first, so a test that overrides `attachments` replaces them
    # instead of passing the keyword twice.
    fields: dict[str, Any] = {"attachments": ATTACHMENTS, **overrides}
    row = await _message(session, project_id=project_id, **fields)
    store.put(row.storage_bucket, row.storage_key, _raw())
    await session.commit()
    return project_id, key, row


# ------------------------------------------------------------- the header ---


@pytest.mark.parametrize(
    ("filename", "expected_plain"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ('a"b.txt', "a_b.txt"),
        ("café.pdf", "caf_.pdf"),
        ("", "attachment"),
        ("..", "attachment"),
    ],
)
def test_content_disposition_is_a_download_whatever_the_name(
    filename: str, expected_plain: str
) -> None:
    header = content_disposition(filename)

    assert header.startswith("attachment; ")
    assert f'filename="{expected_plain}"' in header


@pytest.mark.parametrize("filename", ["x\r\nSet-Cookie: a=b", 'a"; x="y', "a\\b;c=d", "\x00\x01"])
def test_no_name_can_end_the_header_or_start_another(filename: str) -> None:
    header = content_disposition(filename)

    assert "\r" not in header
    assert "\n" not in header
    assert "\x00" not in header
    # The plain form is one quoted string with nothing after the closing quote
    # but the encoded form.
    plain = header.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in plain
    assert ";" not in plain
    assert "\\" not in plain


# ---------------------------------------------------------------- attachment ---


async def test_an_attachment_comes_back_byte_for_byte_as_a_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/attachments/0", headers=_auth(key))

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith('attachment; filename="report.pdf"')
    for name, value in DOWNLOAD_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["X-RateLimit-Limit"]


async def test_what_the_sender_declared_is_never_believed(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """The second file declares itself text/html and contains script. Served from
    the API's own origin as HTML it would run with whatever that origin can do.
    It is served as opaque bytes, in a sandbox, and as a download.
    """
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/attachments/1", headers=_auth(key))

    assert response.status_code == 200
    # Unaltered: the API does not clean what it serves, it contains it.
    assert response.content == HTML_ATTACHMENT
    assert response.headers["content-type"] == "application/octet-stream"
    assert "html" not in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["content-disposition"].startswith("attachment;")


async def test_a_hostile_filename_cannot_reach_a_header_as_anything_but_a_name(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """The second file's name carries a path and a CRLF-delimited Set-Cookie. The
    check is on the bytes of the response, not on how the client reads them.
    """
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/attachments/1", headers=_auth(key))

    disposition = response.headers["content-disposition"]
    assert "set-cookie" not in {name.lower() for name in response.headers}
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "/" not in disposition.split("filename=", 1)[1].split(";", 1)[0]
    assert ".." not in disposition.split('filename="', 1)[1].split('"', 1)[0]


async def test_an_attachment_that_does_not_exist_is_refused_without_a_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/attachments/9", headers=_auth(key))

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"
    assert inbound_store.fetches == []


async def test_a_negative_index_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/attachments/-1", headers=_auth(key))

    assert response.status_code in (400, 404, 422)
    assert inbound_store.fetches == []


# ----------------------------------------------------------------- the raw ---


async def test_the_original_comes_back_unmodified_as_an_eml_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))

    assert response.status_code == 200
    assert response.content == _raw()
    assert response.headers["content-type"] == "message/rfc822"
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="{row.id}.eml"'
    )
    for name, value in DOWNLOAD_HEADERS.items():
        assert response.headers[name] == value


async def test_the_original_is_readable_even_when_nothing_parsed(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """A message that could not be read is the one somebody most wants the
    original of.
    """
    _, key, row = await _stored(
        db_session, inbound_store, parse_failed=True, attachments=[], text_body="", html_body=""
    )

    response = await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))

    assert response.status_code == 200
    assert response.content == _raw()


# ------------------------------------------------------------ who may read ---


@pytest.mark.parametrize("suffix", ["/attachments/0", "/raw"])
async def test_a_download_without_a_key_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore, suffix: str
) -> None:
    _, _, row = await _stored(db_session, inbound_store)

    response = await app_client.get(f"{URL}/{row.id}{suffix}")

    assert response.status_code == 401
    assert inbound_store.fetches == []


@pytest.mark.parametrize("suffix", ["/attachments/0", "/raw"])
async def test_another_projects_message_is_a_404_and_is_never_fetched(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore, suffix: str
) -> None:
    """Ownership is checked before anything is fetched, so a stranger cannot make
    the server read somebody else's mail, even to be told it is not theirs.
    """
    _, _, theirs = await _stored(db_session, inbound_store, owner="theirs@example.com")
    _, mine = await _project_key(db_session, owner="mine@example.com")
    await db_session.commit()

    foreign = await app_client.get(f"{URL}/{theirs.id}{suffix}", headers=_auth(mine))
    unknown = await app_client.get(f"{URL}/inbound_01NEVEREXISTED{suffix}", headers=_auth(mine))

    assert foreign.status_code == 404
    assert foreign.json() == unknown.json()
    assert inbound_store.fetches == []


# ------------------------------------------------------ the original's life ---


@pytest.mark.parametrize("suffix", ["/attachments/0", "/raw"])
async def test_a_message_past_retention_is_refused_without_asking_s3(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore, suffix: str
) -> None:
    """After the date the original is gone or about to be, and a round trip to
    confirm can only produce the same answer.
    """
    _, key, row = await _stored(
        db_session, inbound_store, raw_expires_at=datetime.now(UTC) - timedelta(days=1)
    )

    response = await app_client.get(f"{URL}/{row.id}{suffix}", headers=_auth(key))

    assert response.status_code == 404
    assert "no longer in storage" in response.json()["error"]["message"]
    assert inbound_store.fetches == []


async def test_an_original_that_expired_early_says_so_rather_than_failing(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """Retention removes objects at a day boundary, so the date can say "still
    there" while the bucket says otherwise. That is a 404 with the same message,
    not a server error.
    """
    _, key, row = await _stored(db_session, inbound_store)
    inbound_store.objects.clear()

    response = await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == EXPIRED
    assert len(inbound_store.fetches) == 1


async def test_the_parsed_message_outlives_the_download(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    _, key, row = await _stored(
        db_session, inbound_store, raw_expires_at=datetime.now(UTC) - timedelta(days=1)
    )

    assert (await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))).status_code == 404
    assert (await app_client.get(f"{URL}/{row.id}", headers=_auth(key))).status_code == 200


# --------------------------------------------------------- AWS credentials ---


@pytest.mark.parametrize("suffix", ["/attachments/0", "/raw"])
async def test_a_project_that_is_not_connected_is_told_so_and_nothing_is_fetched(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore, suffix: str
) -> None:
    _, key, row = await _stored(db_session, inbound_store, connected=False)

    response = await app_client.get(f"{URL}/{row.id}{suffix}", headers=_auth(key))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert "not connected to AWS" in response.json()["error"]["message"]
    assert inbound_store.fetches == []


async def test_a_message_is_read_with_its_own_projects_key_in_its_own_region(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """Credentials belong to a project, never to the instance. Reading a message
    on any other key would be a cross-account read.
    """
    _, key, row = await _stored(db_session, inbound_store)

    await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))

    assert inbound_store.built_with is not None
    region, credentials = inbound_store.built_with
    assert region == "us-east-1"
    assert credentials.access_key_id == FAKE_CREDENTIALS.access_key_id


async def test_a_permission_failure_is_reported_as_one_not_as_a_missing_message(
    app_client: AsyncClient, db_session: AsyncSession, inbound_store: FakeInboundStore
) -> None:
    """A missing s3:GetObject must not read as "the message is gone" - the user
    would conclude their mail was lost when it is a policy to fix.
    """
    _, key, row = await _stored(db_session, inbound_store)
    inbound_store.error = APIError(
        ErrorType.AUTHORIZATION_FAILED, "not permitted to call s3:GetObject"
    )

    response = await app_client.get(f"{URL}/{row.id}/raw", headers=_auth(key))

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "authorization_failed"
