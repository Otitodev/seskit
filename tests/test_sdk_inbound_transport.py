"""The SDK's inbound calls, against a stub server (inbound email, Phase C).

What a request looks like and what a response becomes, with no real server, so
these run anywhere and can assert on the exact bytes that would have gone out.
``tests/test_sdk_inbound_contract.py`` is the other half: the same client pointed
at the real application.

Two things are specific to this resource. A download is *bytes*, and the transport
has to hand them back untouched - decoding them as JSON fails and decoding them as
text corrupts them. And an id is interpolated into a URL, so what an id can reach
is worth a test of its own.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from seskit import (
    AsyncSesKit,
    InboundEmail,
    InboundPage,
    InboundSummary,
    NotFound,
    SesKit,
    Verdicts,
)
from test_sdk_transport import API_KEY, BASE_URL, Recorder, _client, _error, _ok

SUMMARY: dict[str, Any] = {
    "id": "inbound_01J8XQ",
    "domain": "example.com",
    "from": "ada@other.org",
    "from_name": "Ada Lovelace",
    "to": ["support@example.com"],
    "cc": [],
    "subject": "Hello there",
    "received_at": "2026-10-05T10:00:00+00:00",
    "size": 4821,
    "attachment_count": 1,
    "verdicts": {"spf": "PASS", "dkim": "GRAY", "dmarc": None, "spam": "PASS", "virus": None},
    "truncated": False,
    "parse_failed": False,
    "raw_available": True,
    "raw_expires_at": "2026-11-04T10:00:00+00:00",
}

FULL: dict[str, Any] = {
    **SUMMARY,
    "text": "Hi.",
    "html": "<p>Hi.</p>",
    "envelope_from": "bounce@other.org",
    "recipients": ["support@example.com", "bcc@example.com"],
    "reply_to": ["replies@other.org"],
    "message_id": "abc@other.org",
    "in_reply_to": "first@example.com",
    "references": ["root@example.com", "first@example.com"],
    "attachments": [
        {
            "index": 0,
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size": 9,
            "inline": False,
            "content_id": None,
        }
    ],
    "headers": [{"name": "Subject", "value": "Hello there"}],
}

#: Not text, not UTF-8, with a NUL in it: what a PDF looks like to a decoder.
BINARY = b"%PDF-1.4\n\x00\xff\xfe\x80binary\r\n"


def _bytes(content: bytes = BINARY, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=content)


# --------------------------------------------------------------- reading ---


def test_a_received_message_becomes_an_inbound_email() -> None:
    recorder = Recorder(_ok(FULL))

    message = _client(recorder).inbound.get("inbound_01J8XQ")

    assert isinstance(message, InboundEmail)
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/v1/inbound/inbound_01J8XQ"
    assert message.id == "inbound_01J8XQ"
    # `from` is a keyword, so the attribute carries a trailing underscore.
    assert message.from_ == "ada@other.org"
    assert message.text == "Hi."
    assert message.html == "<p>Hi.</p>"
    assert message.recipients == ["support@example.com", "bcc@example.com"]
    assert message.references == ["root@example.com", "first@example.com"]
    assert message.attachments[0].filename == "report.pdf"
    assert message.attachments[0].index == 0
    assert message.headers == [("Subject", "Hello there")]
    assert message.received_at is not None
    assert message.received_at.year == 2026


def test_a_verdict_the_api_left_null_stays_none_and_is_never_a_pass() -> None:
    message = _client(Recorder(_ok(FULL))).inbound.get("inbound_01J8XQ")

    assert message.verdicts == Verdicts(
        spf="PASS", dkim="GRAY", dmarc=None, spam="PASS", virus=None
    )
    assert message.verdicts.virus is None
    assert message.verdicts.dmarc_policy is None


def test_a_page_is_summaries_and_carries_no_body() -> None:
    recorder = Recorder(
        _ok({"data": [SUMMARY, {**SUMMARY, "id": "inbound_01J8XR"}], "has_more": True})
    )

    page = _client(recorder).inbound.list()

    assert isinstance(page, InboundPage)
    assert len(page) == 2
    assert page.has_more is True
    assert page.last_id == "inbound_01J8XR"
    assert all(isinstance(row, InboundSummary) for row in page)
    assert not any(isinstance(row, InboundEmail) for row in page)
    assert not hasattr(page.data[0], "html")


def test_an_empty_page_has_no_last_id() -> None:
    page = _client(Recorder(_ok({"data": [], "has_more": False}))).inbound.list()

    assert page.last_id is None
    assert len(page) == 0


def test_list_sends_only_the_filters_it_was_given() -> None:
    recorder = Recorder(_ok({"data": [], "has_more": False}))
    client = _client(recorder)

    client.inbound.list()
    client.inbound.list(limit=5, starting_after="inbound_x", domain="example.com")

    assert dict(recorder.requests[0].url.params) == {}
    assert dict(recorder.requests[1].url.params) == {
        "limit": "5",
        "starting_after": "inbound_x",
        "domain": "example.com",
    }


def test_a_field_this_version_does_not_know_is_kept() -> None:
    """A client from before a field existed still hands it to the caller."""
    message = _client(Recorder(_ok({**FULL, "thread_id": "t_1"}))).inbound.get("inbound_01J8XQ")

    assert message.raw["thread_id"] == "t_1"


def test_a_sparse_payload_still_reads() -> None:
    message = _client(Recorder(_ok({"id": "inbound_1"}))).inbound.get("inbound_1")

    assert message.id == "inbound_1"
    assert message.attachments == []
    assert message.headers == []
    assert message.verdicts == Verdicts()
    assert message.received_at is None


# ------------------------------------------------------------- downloads ---


def test_an_attachment_is_the_bytes_untouched() -> None:
    """Not decoded as JSON, which would fail, and not as text, which would
    corrupt it: a NUL, an 0xff and a lone 0x80 all have to survive.
    """
    recorder = Recorder(_bytes())

    data = _client(recorder).inbound.attachment("inbound_01J8XQ", 0)

    assert data == BINARY
    assert isinstance(data, bytes)
    request = recorder.requests[0]
    assert request.url.path == "/v1/inbound/inbound_01J8XQ/attachments/0"
    # A download does not claim to want JSON.
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"


def test_the_original_is_the_bytes_untouched() -> None:
    recorder = Recorder(_bytes(b"From: a@b.org\r\n\r\n\xff\x00body"))

    data = _client(recorder).inbound.raw("inbound_01J8XQ")

    assert data == b"From: a@b.org\r\n\r\n\xff\x00body"
    assert recorder.requests[0].url.path == "/v1/inbound/inbound_01J8XQ/raw"


def test_a_download_that_fails_is_still_a_typed_error() -> None:
    """Errors are JSON envelopes whether or not a file was asked for."""
    recorder = Recorder(_error(404, "not_found", "That message is no longer in storage."))

    with pytest.raises(NotFound) as caught:
        _client(recorder).inbound.raw("inbound_01J8XQ")

    assert "no longer in storage" in str(caught.value)


def test_a_download_is_tried_again_on_a_server_error() -> None:
    """It shares the retry rule with every other call."""
    recorder = Recorder(_error(500, "internal_error"), _bytes())

    assert _client(recorder).inbound.raw("inbound_01J8XQ") == BINARY
    assert len(recorder.requests) == 2


def test_a_refusal_is_not_retried_when_a_file_was_asked_for() -> None:
    recorder = Recorder(_error(404, "not_found"), _bytes())

    with pytest.raises(NotFound):
        _client(recorder).inbound.attachment("inbound_01J8XQ", 0)

    assert len(recorder.requests) == 1


# ------------------------------------------------- what an id can reach ---


def test_an_id_cannot_leave_its_path_segment() -> None:
    """Ids go into a URL, and ``../emails`` is a valid string. Quoted, it stays
    one segment, which the API will say it does not know - and never a different
    endpoint answering with a message that parses as the wrong thing.
    """
    recorder = Recorder(_error(404, "not_found"))

    with pytest.raises(NotFound):
        _client(recorder).inbound.get("../emails")

    assert recorder.requests[0].url.raw_path == b"/v1/inbound/..%2Femails"


@pytest.mark.parametrize("bad_id", ["a/b", "a?x=1", "a#frag", "a b", "\r\nX: y"])
def test_no_id_adds_a_segment_a_query_or_a_header(bad_id: str) -> None:
    recorder = Recorder(_error(404, "not_found"))

    with pytest.raises(NotFound):
        _client(recorder).inbound.raw(bad_id)

    request = recorder.requests[0]
    assert request.url.raw_path.count(b"/") == 4
    assert request.url.query == b""
    assert request.url.fragment == ""
    assert "X" not in request.headers


def test_an_index_that_is_not_a_number_never_reaches_the_url() -> None:
    recorder = Recorder(_bytes())

    with pytest.raises(ValueError):
        _client(recorder).inbound.attachment("inbound_01J8XQ", "0/../../raw")  # type: ignore[arg-type]

    assert recorder.requests == []


# ------------------------------------------------- the two clients agree ---


async def test_the_async_client_sends_the_same_request_and_returns_the_same_bytes() -> None:
    """What is proved here and in the contract file holds for both: the request
    builders, the URL, the headers and the transport rule are shared.
    """
    sync_recorder = Recorder(_bytes())
    async_recorder = Recorder(_bytes())
    sync_client: SesKit = _client(sync_recorder)
    async_client = AsyncSesKit(
        api_key=API_KEY,
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(async_recorder)),
    )

    from_sync = sync_client.inbound.attachment("inbound_01J8XQ", 2)
    from_async = await async_client.inbound.attachment("inbound_01J8XQ", 2)

    assert from_sync == from_async == BINARY
    one, two = sync_recorder.requests[0], async_recorder.requests[0]
    assert (one.method, one.url, one.content) == (two.method, two.url, two.content)
    assert one.headers["Accept"] == two.headers["Accept"] == "*/*"


async def test_the_async_client_reads_a_message_the_same_way() -> None:
    client = AsyncSesKit(
        api_key=API_KEY,
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(Recorder(_ok(FULL)))),
    )

    message = await client.inbound.get("inbound_01J8XQ")

    assert message.subject == "Hello there"
    assert message.attachments[0].filename == "report.pdf"
