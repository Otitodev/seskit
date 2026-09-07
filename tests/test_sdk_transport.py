"""The SDK's transport, against a stub server (§13, §31 Phase 12).

What a request looks like, what a response becomes, and when the client tries
again. No real server: `httpx.MockTransport` answers, so these run in
milliseconds and can assert on the exact bytes that would have gone out.

`tests/test_sdk_contract.py` is the other half and the more important one — it
points the same client at the real application. These two tests answer
different questions: this one asks "does the client send what it thinks it
sends", that one asks "is what it thinks right".
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from seskit import (
    AsyncSesKit,
    DomainNotVerified,
    NotFound,
    SesKit,
    SESKitConnectionError,
    SESKitError,
    SuppressedRecipient,
)
from seskit._errors import ERROR_CLASSES

API_KEY = "sk_live_not_a_real_key"
BASE_URL = "https://seskit.example.com"

SENT = {"id": "email_01J8XQ", "status": "queued"}

STORED = {
    "id": "email_01J8XQ",
    "status": "sent",
    "from": "Acme <hello@example.com>",
    "to": ["user@example.com"],
    "cc": [],
    "reply_to": [],
    "subject": "Welcome",
    "html": "<h1>Welcome!</h1>",
    "text": None,
    "headers": {"X-Entity-Ref-Id": "order-1234"},
    "provider_message_id": "0100018f",
    "last_error": None,
    "created_at": "2026-09-02T09:00:00+00:00",
    "sent_at": "2026-09-02T09:00:01+00:00",
    "delivered_at": None,
}


class Recorder:
    """A stub server that remembers what it was asked."""

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # The last answer repeats, so a test that only cares about the request
        # does not have to count how many times it will be made.
        return self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]

    @property
    def body(self) -> dict[str, Any]:
        return dict(json.loads(self.requests[0].content))


def _ok(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _error(status_code: int, error_type: str, message: str = "No.") -> httpx.Response:
    return httpx.Response(status_code, json={"error": {"type": error_type, "message": message}})


def _client(recorder: Recorder, **kwargs: Any) -> SesKit:
    return SesKit(
        api_key=API_KEY,
        base_url=kwargs.pop("base_url", BASE_URL),
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff is tested by asserting the number it computes, not by living
    through it. A suite that really slept would be slower than the thing it is
    testing.
    """
    monkeypatch.setattr("seskit._client.time.sleep", lambda _seconds: None)


# ------------------------------------------------------------- addressing ---


def test_the_key_goes_out_as_a_bearer_token() -> None:
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert recorder.requests[0].headers["Authorization"] == f"Bearer {API_KEY}"


def test_the_client_names_itself() -> None:
    """So an operator reading their own access log can tell SDK traffic from
    somebody's curl.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert recorder.requests[0].headers["User-Agent"].startswith("seskit-python/")


def test_a_trailing_slash_on_the_base_url_does_not_double() -> None:
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder, base_url=f"{BASE_URL}/").emails.send(
        from_="a@example.com", to="b@example.com", subject="Hi", text="x"
    )

    assert str(recorder.requests[0].url) == f"{BASE_URL}/v1/emails"


def test_a_client_without_a_base_url_is_refused_before_it_sends_anything() -> None:
    """No default. There is no hosted SESKit to point at, and defaulting to
    localhost would turn a forgotten argument into mail that goes nowhere.
    """
    with pytest.raises(ValueError, match="base_url"):
        SesKit(api_key=API_KEY, base_url="")


def test_a_client_without_a_key_is_refused() -> None:
    with pytest.raises(ValueError, match="api_key"):
        SesKit(api_key="", base_url=BASE_URL)


# ----------------------------------------------------------------- bodies ---


def test_a_single_recipient_is_sent_as_a_list() -> None:
    """The API takes either. Normalising means a caller who passes a string and
    one who passes a list produce identical requests, so a bug cannot depend on
    which they chose.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert recorder.body["to"] == ["b@example.com"]


def test_from_is_sent_under_its_real_name() -> None:
    """`from` is a keyword in Python and a field name in the API. The
    underscore is a Python problem and must not reach the wire.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert recorder.body["from"] == "a@example.com"
    assert "from_" not in recorder.body


def test_what_was_not_asked_for_is_not_sent() -> None:
    """Omitted rather than sent as null, so a request carries what the caller
    actually asked for and two requests diff readably.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert set(recorder.body) == {"from", "to", "subject", "text"}


def test_an_attachment_is_encoded_for_the_wire() -> None:
    """JSON has no byte type. Encoding is the wire format, not a rule, which is
    why this is the one transformation §13 allows the client.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(
        from_="a@example.com",
        to="b@example.com",
        subject="Hi",
        text="x",
        attachments=[("report.csv", b"a,b\n1,2\n", "text/csv")],
    )

    attachment = recorder.body["attachments"][0]
    assert attachment == {
        "filename": "report.csv",
        "content": "YSxiCjEsMgo=",
        "content_type": "text/csv",
    }


# ------------------------------------------------------------ idempotency ---


def test_every_send_carries_an_idempotency_key() -> None:
    """Generated rather than left out, because this client retries. Without a
    key, a retry after a timeout delivers a second copy of a message the server
    already accepted.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert recorder.requests[0].headers["Idempotency-Key"]


def test_a_key_the_caller_chose_is_the_one_sent() -> None:
    """An order id is a better key than a random one: it deduplicates across
    process restarts, which a key generated per call cannot.
    """
    recorder = Recorder(_ok(SENT, 201))

    _client(recorder).emails.send(
        from_="a@example.com",
        to="b@example.com",
        subject="Hi",
        text="x",
        idempotency_key="order-1234",
    )

    assert recorder.requests[0].headers["Idempotency-Key"] == "order-1234"


def test_two_sends_do_not_share_a_key() -> None:
    recorder = Recorder(_ok(SENT, 201))
    client = _client(recorder)

    client.emails.send(from_="a@example.com", to="b@example.com", subject="One", text="x")
    client.emails.send(from_="a@example.com", to="b@example.com", subject="Two", text="x")

    first, second = (request.headers["Idempotency-Key"] for request in recorder.requests)
    assert first != second


# --------------------------------------------------------------- reading ---


def test_a_stored_message_becomes_an_email() -> None:
    recorder = Recorder(_ok(STORED))

    email = _client(recorder).emails.get("email_01J8XQ")

    assert email.id == "email_01J8XQ"
    assert email.from_ == "Acme <hello@example.com>"
    assert email.created_at is not None
    assert email.delivered_at is None
    assert str(recorder.requests[0].url) == f"{BASE_URL}/v1/emails/email_01J8XQ"


def test_a_page_carries_its_rows_and_whether_there_are_more() -> None:
    recorder = Recorder(_ok({"data": [STORED], "has_more": True}))

    page = _client(recorder).emails.list(limit=1)

    assert len(page) == 1
    assert page.has_more is True
    assert page.last_id == "email_01J8XQ"
    assert [email.id for email in page] == ["email_01J8XQ"]


def test_list_sends_only_the_filters_it_was_given() -> None:
    recorder = Recorder(_ok({"data": [], "has_more": False}))

    _client(recorder).emails.list(status="failed")

    assert dict(recorder.requests[0].url.params) == {"status": "failed"}


def test_the_custom_headers_reach_the_caller() -> None:
    """Echoed by the API since Phase 12; useless if the client drops them on
    the way through.
    """
    recorder = Recorder(_ok(STORED))

    email = _client(recorder).emails.get("email_01J8XQ")

    assert email.headers == {"X-Entity-Ref-Id": "order-1234"}


def test_a_message_with_no_headers_reads_back_empty() -> None:
    """`None` would make every caller check before iterating."""
    recorder = Recorder(_ok({**STORED, "headers": None}))

    email = _client(recorder).emails.get("email_01J8XQ")

    assert email.headers == {}


def test_a_field_this_version_does_not_know_is_kept() -> None:
    """A client from before a field existed should hand it to the caller rather
    than swallowing it.
    """
    recorder = Recorder(_ok({**STORED, "opened_at": "2026-09-02T10:00:00+00:00"}))

    email = _client(recorder).emails.get("email_01J8XQ")

    assert email.raw["opened_at"] == "2026-09-02T10:00:00+00:00"


# ---------------------------------------------------------------- errors ---


def test_a_refusal_becomes_the_class_that_names_it() -> None:
    """The point of the whole error module: a caller writes `except
    SuppressedRecipient` rather than comparing strings.
    """
    recorder = Recorder(_error(422, "suppressed_recipient", "user@example.com is on the list."))

    with pytest.raises(SuppressedRecipient) as raised:
        _client(recorder).emails.send(
            from_="a@example.com", to="b@example.com", subject="Hi", text="x"
        )

    assert raised.value.status_code == 422
    assert raised.value.type == "suppressed_recipient"
    assert "on the list" in raised.value.message


def test_every_refusal_can_be_caught_as_one_thing() -> None:
    recorder = Recorder(_error(422, "domain_not_verified"))

    with pytest.raises(SESKitError):
        _client(recorder).emails.send(
            from_="a@example.com", to="b@example.com", subject="Hi", text="x"
        )


def test_a_missing_message_is_not_found() -> None:
    recorder = Recorder(_error(404, "not_found"))

    with pytest.raises(NotFound):
        _client(recorder).emails.get("email_nope")


def test_an_error_type_this_version_never_heard_of_still_raises() -> None:
    """A client older than the server it is talking to. A KeyError from inside
    the SDK would tell the caller nothing; the base class tells them the API
    refused and what it said.
    """
    recorder = Recorder(_error(418, "brewed_coffee", "Not possible."))

    with pytest.raises(SESKitError) as raised:
        _client(recorder).emails.get("email_01J8XQ")

    assert raised.value.type == "brewed_coffee"
    assert raised.value.status_code == 418


def test_a_body_that_is_not_the_documented_envelope_still_raises() -> None:
    """A proxy in front of the instance returning its own HTML 502, or a URL
    that is not a SESKit at all. Must not become a TypeError from inside the
    SDK, which says nothing about what went wrong.
    """
    recorder = Recorder(httpx.Response(502, text="<html>Bad Gateway</html>"))

    with pytest.raises(SESKitError) as raised:
        _client(recorder).emails.get("email_01J8XQ")

    assert raised.value.status_code == 502


def test_every_error_the_api_can_raise_has_a_class() -> None:
    """The guard that keeps the duplication honest. The SDK cannot import
    `seskit_core` - that would drag the server into every application that
    wants to send an email - so the type names are written twice, and this is
    what stops the second copy going stale.
    """
    from seskit_core.errors import ErrorType

    missing = sorted(item.value for item in ErrorType if item.value not in ERROR_CLASSES)

    assert not missing, f"error types the SDK has no class for: {missing}"


# ---------------------------------------------------------------- retries ---


def test_a_server_error_is_tried_again() -> None:
    recorder = Recorder(httpx.Response(503), _ok(SENT, 201))

    accepted = _client(recorder).emails.send(
        from_="a@example.com", to="b@example.com", subject="Hi", text="x"
    )

    assert accepted.id == "email_01J8XQ"
    assert len(recorder.requests) == 2


def test_a_rate_limit_is_tried_again() -> None:
    recorder = Recorder(_error(429, "rate_limit_exceeded"), _ok(SENT, 201))

    _client(recorder).emails.send(from_="a@example.com", to="b@example.com", subject="Hi", text="x")

    assert len(recorder.requests) == 2


def test_a_refusal_is_not_tried_again() -> None:
    """Retrying a 422 produces a second 422. The client decides whether asking
    again could change the answer - never what the answer meant.
    """
    recorder = Recorder(_error(422, "domain_not_verified"))

    with pytest.raises(DomainNotVerified):
        _client(recorder).emails.send(
            from_="a@example.com", to="b@example.com", subject="Hi", text="x"
        )

    assert len(recorder.requests) == 1


def test_giving_up_raises_what_the_server_said() -> None:
    """Not a generic "gave up after 3 tries", which loses the only useful part
    of the exchange.
    """
    recorder = Recorder(_error(503, "internal_error", "Database is down."))

    with pytest.raises(SESKitError) as raised:
        _client(recorder, max_attempts=2).emails.get("email_01J8XQ")

    assert len(recorder.requests) == 2
    assert "Database is down." in raised.value.message


def test_the_server_decides_how_long_to_wait() -> None:
    """`Retry-After` wins over the client's backoff. The server knows when its
    own window resets, and guessing shorter spends another request being
    refused.
    """
    from seskit._client import _Transport

    transport = _Transport(api_key=API_KEY, base_url=BASE_URL)
    response = httpx.Response(429, headers={"Retry-After": "12"})

    assert transport.backoff(response, attempt=0) == 12.0


def test_a_backoff_is_capped() -> None:
    """A client that sleeps for a minute inside somebody's request handler is
    worse than one that gives up and lets them decide.
    """
    from seskit._client import _Transport
    from seskit._transport import MAX_BACKOFF_SECONDS

    transport = _Transport(api_key=API_KEY, base_url=BASE_URL)

    assert transport.backoff(None, attempt=20) == MAX_BACKOFF_SECONDS


def test_a_server_that_cannot_be_reached_is_its_own_failure() -> None:
    """Distinct from every refusal, which are answers. This is the absence of
    one, and a send that fails this way may or may not have been accepted -
    which is what the idempotency key is for.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = SesKit(
        api_key=API_KEY,
        base_url=BASE_URL,
        max_attempts=2,
        http_client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )

    with pytest.raises(SESKitConnectionError) as raised:
        client.emails.get("email_01J8XQ")

    assert BASE_URL in raised.value.message


# ------------------------------------------------------------------ async ---


def _async_client(recorder: Recorder, **kwargs: Any) -> AsyncSesKit:
    return AsyncSesKit(
        api_key=API_KEY,
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        **kwargs,
    )


async def test_the_async_client_sends_the_same_request() -> None:
    """The two clients share the request builder, and this is what says so. If
    they ever diverge - a header on one and not the other - that is a bug a
    caller would only find by switching between them.
    """
    sync_recorder = Recorder(_ok(SENT, 201))
    async_recorder = Recorder(_ok(SENT, 201))

    _client(sync_recorder).emails.send(
        from_="a@example.com", to="b@example.com", subject="Hi", text="x", idempotency_key="k"
    )
    await _async_client(async_recorder).emails.send(
        from_="a@example.com", to="b@example.com", subject="Hi", text="x", idempotency_key="k"
    )

    synchronous, asynchronous = sync_recorder.requests[0], async_recorder.requests[0]
    assert synchronous.content == asynchronous.content
    assert str(synchronous.url) == str(asynchronous.url)
    assert synchronous.headers["Idempotency-Key"] == asynchronous.headers["Idempotency-Key"]


async def test_the_async_client_reads_a_message() -> None:
    recorder = Recorder(_ok(STORED))

    email = await _async_client(recorder).emails.get("email_01J8XQ")

    assert email.id == "email_01J8XQ"
    assert email.from_ == "Acme <hello@example.com>"


async def test_the_async_client_pages() -> None:
    recorder = Recorder(_ok({"data": [STORED], "has_more": False}))

    page = await _async_client(recorder).emails.list(limit=1)

    assert page.has_more is False
    assert [email.id for email in page] == ["email_01J8XQ"]


async def test_the_async_client_maps_errors_the_same_way() -> None:
    recorder = Recorder(_error(422, "suppressed_recipient", "On the list."))

    with pytest.raises(SuppressedRecipient):
        await _async_client(recorder).emails.send(
            from_="a@example.com", to="b@example.com", subject="Hi", text="x"
        )


async def test_the_async_client_retries_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.sleep`, not `time.sleep`. Blocking the loop in the backoff of
    the client that exists not to block the loop would be the whole point,
    missed.
    """
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("seskit._async_client.asyncio.sleep", _record)
    recorder = Recorder(httpx.Response(503), _ok(SENT, 201))

    await _async_client(recorder).emails.send(
        from_="a@example.com", to="b@example.com", subject="Hi", text="x"
    )

    assert len(recorder.requests) == 2
    assert slept == [0.5]


async def test_closing_an_async_client_is_awaited() -> None:
    """A sync `close()` on an async client would be a footgun: it would look
    like it worked and leave the pool open.
    """
    recorder = Recorder(_ok(STORED))
    client = _async_client(recorder)

    await client.aclose()

    assert not hasattr(client, "close")
