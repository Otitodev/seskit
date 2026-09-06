"""The SDK against the real API (§13, §31 Phase 12).

`tests/test_sdk_transport.py` asserts the client sends what it thinks it sends.
This asserts that what it thinks is right — the same client, pointed at the
actual application, with the actual Postgres and Redis behind it.

**This is the tier that catches the mistakes that matter.** A mocked SDK test
is written from the author's memory of the API, and a stub that agrees with a
misremembering passes forever. Here a wrong field name is a 422, a wrong header
name means the idempotency key silently stops deduplicating, and a renamed
error type means an exception nobody can catch.

The async client carries these because `httpx.ASGITransport` is async-only.
That is not a gap: the two clients share the request builder, the URL, the
headers, the retry rule and the error mapping, and a transport test asserts a
call through each produces identical bytes. What is proved once here holds for
both.
"""

from __future__ import annotations

from httpx import AsyncClient
from seskit import (
    AsyncSesKit,
    AuthenticationFailed,
    InvalidRequest,
    NotFound,
    SuppressedRecipient,
)
from seskit_core.models import Email, EmailStatus, SuppressionReason
from seskit_core.services import create_api_key, create_project, register_user, suppress
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
SENDER = "Acme <hello@example.com>"
RECIPIENT = "user@example.com"

#: `app_client` is an ASGI transport, so its own base_url is never used - the
#: SDK builds absolute URLs. This is what those URLs are rooted at.
BASE_URL = "http://test"


async def _key(session: AsyncSession, *, owner: str = "owner@example.com") -> tuple[str, str]:
    """A project and a usable API key. Returns ``(project_id, raw_key)``."""
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    issued = await create_api_key(session, project_id=project.id, name="prod")
    return project.id, issued.raw_key


def _sdk(app_client: AsyncClient, raw_key: str) -> AsyncSesKit:
    """The published client, wired to the running application.

    Nothing is stubbed on the SDK side: this is the object a user gets from
    `pip install seskit`, given somebody else's HTTP transport.
    """
    return AsyncSesKit(api_key=raw_key, base_url=BASE_URL, http_client=app_client)


# ----------------------------------------------------------------- sending ---


async def test_a_send_through_the_sdk_lands_a_real_message(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, raw_key = await _key(db_session)

    accepted = await _sdk(app_client, raw_key).emails.send(
        from_=SENDER, to=[RECIPIENT], subject="Welcome", html="<h1>Welcome!</h1>"
    )

    assert accepted.status == EmailStatus.QUEUED.value
    stored = await db_session.scalar(select(Email).where(Email.id == accepted.id))
    assert stored is not None
    assert stored.project_id == project_id
    assert stored.to_addresses == [RECIPIENT]


async def test_a_single_recipient_string_reaches_the_row_as_a_list(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The client normalises a string to a list. This is what proves the API
    agrees rather than storing something else.
    """
    _, raw_key = await _key(db_session)

    accepted = await _sdk(app_client, raw_key).emails.send(
        from_=SENDER, to=RECIPIENT, subject="Welcome", text="Hello"
    )

    stored = await db_session.scalar(select(Email).where(Email.id == accepted.id))
    assert stored is not None
    assert stored.to_addresses == [RECIPIENT]


async def test_an_attachment_survives_the_round_trip(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The client base64-encodes; the API decodes. A mismatch either way would
    deliver a corrupt file, which nothing else in the suite would notice.
    """
    _, raw_key = await _key(db_session)

    accepted = await _sdk(app_client, raw_key).emails.send(
        from_=SENDER,
        to=[RECIPIENT],
        subject="Report",
        text="Attached",
        attachments=[("report.csv", b"a,b\n1,2\n", "text/csv")],
    )

    stored = await db_session.scalar(select(Email).where(Email.id == accepted.id))
    assert stored is not None
    assert [(item.filename, item.content) for item in stored.attachments] == [
        ("report.csv", b"a,b\n1,2\n")
    ]


async def test_the_sdk_s_idempotency_key_really_deduplicates(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The header name has to be exactly right, and nothing else would say so.

    If the client sent `Idempotency-Key` under any other spelling, every retry
    it makes would deliver a second copy of the message - silently, and only in
    production, where timeouts happen.
    """
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)

    first = await client.emails.send(
        from_=SENDER, to=[RECIPIENT], subject="Welcome", text="Hello", idempotency_key="order-1234"
    )
    second = await client.emails.send(
        from_=SENDER, to=[RECIPIENT], subject="Welcome", text="Hello", idempotency_key="order-1234"
    )

    assert first.id == second.id


# ----------------------------------------------------------------- reading ---


async def test_a_sent_message_reads_back(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Every field name in `Email.from_payload` is checked here at once: a
    renamed one comes back empty rather than raising.
    """
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)
    accepted = await client.emails.send(
        from_=SENDER, to=[RECIPIENT], subject="Welcome", html="<h1>Welcome!</h1>"
    )

    email = await client.emails.get(accepted.id)

    assert email.id == accepted.id
    assert email.from_ == SENDER
    assert email.to == [RECIPIENT]
    assert email.subject == "Welcome"
    assert email.html == "<h1>Welcome!</h1>"
    assert email.created_at is not None


async def test_a_bcc_is_not_readable_through_the_sdk(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A blind copy is blind through every door. The SDK has no attribute for
    it; this checks it is not hiding in `raw` either.
    """
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)
    accepted = await client.emails.send(
        from_=SENDER,
        to=[RECIPIENT],
        subject="Welcome",
        text="Hello",
        bcc=["archive@example.com"],
    )

    email = await client.emails.get(accepted.id)

    assert "archive@example.com" not in str(email.raw)


async def test_a_page_lists_what_was_sent(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """That the rows arrive and parse. Ordering is asserted in
    `test_v1_emails_list.py`, where the ids can be given timestamps a second
    apart - two sends here can land in the same millisecond, and inside one
    millisecond a ULID's order is decided by its random bits.
    """
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)
    first = await client.emails.send(from_=SENDER, to=[RECIPIENT], subject="First", text="one")
    second = await client.emails.send(from_=SENDER, to=[RECIPIENT], subject="Second", text="two")

    page = await client.emails.list()

    assert {email.id for email in page} == {first.id, second.id}
    assert page.has_more is False


async def test_paging_with_the_cursor_the_sdk_hands_back(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`page.last_id` has to be what `starting_after` expects. A cursor the API
    rejects is a 404, and a cursor it silently misreads is a page of the wrong
    messages.
    """
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)
    for index in range(3):
        await client.emails.send(from_=SENDER, to=[RECIPIENT], subject=f"Message {index}", text="x")

    first_page = await client.emails.list(limit=2)
    assert first_page.last_id is not None
    second_page = await client.emails.list(limit=2, starting_after=first_page.last_id)

    assert first_page.has_more is True
    assert len(second_page) == 1
    assert second_page.has_more is False


async def test_the_status_filter_reaches_the_query(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _key(db_session)
    client = _sdk(app_client, raw_key)
    await client.emails.send(from_=SENDER, to=[RECIPIENT], subject="Queued", text="x")

    page = await client.emails.list(status="failed")

    assert len(page) == 0


# ------------------------------------------------------------------ errors ---


async def test_a_suppressed_address_raises_the_class_that_names_it(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Phase 11's refusal, reaching a Python caller as something they can act
    on. Anything less specific and the only way to tell this apart from a
    validation failure would be to read the message.
    """
    project_id, raw_key = await _key(db_session)
    await suppress(
        db_session, project_id=project_id, address=RECIPIENT, reason=SuppressionReason.BOUNCE
    )
    await db_session.commit()

    try:
        await _sdk(app_client, raw_key).emails.send(
            from_=SENDER, to=[RECIPIENT], subject="Welcome", text="Hello"
        )
    except SuppressedRecipient as error:
        assert error.status_code == 422
        assert RECIPIENT in error.message
    else:  # pragma: no cover - the send should never be accepted
        raise AssertionError("a suppressed address was accepted")


async def test_a_body_the_api_will_not_take_raises_invalid_request(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A message with neither html nor text. Also the end-to-end check on the
    validation envelope: the API's own handler produces `invalid_request`, and
    the SDK has a class for it.
    """
    _, raw_key = await _key(db_session)

    try:
        await _sdk(app_client, raw_key).emails.send(from_=SENDER, to=[RECIPIENT], subject="Empty")
    except InvalidRequest as error:
        assert error.type == "invalid_request"
    else:  # pragma: no cover
        raise AssertionError("a body with no content was accepted")


async def test_an_unknown_id_raises_not_found(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, raw_key = await _key(db_session)

    try:
        await _sdk(app_client, raw_key).emails.get("email_01NOPE")
    except NotFound as error:
        assert error.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("a missing message was returned")


async def test_a_key_that_is_not_a_key_raises_authentication_failed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _key(db_session)

    try:
        await _sdk(app_client, "sk_live_wrong").emails.list()
    except AuthenticationFailed as error:
        assert error.status_code == 401
    else:  # pragma: no cover
        raise AssertionError("an invalid key was accepted")


async def test_a_key_cannot_read_another_project(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The guarantee every /v1 route makes, checked from the outside - through
    the client somebody would actually use.
    """
    _, mine = await _key(db_session, owner="owner@example.com")
    _, theirs = await _key(db_session, owner="other@example.com")
    stranger = await _sdk(app_client, theirs).emails.send(
        from_=SENDER, to=[RECIPIENT], subject="Theirs", text="x"
    )

    try:
        await _sdk(app_client, mine).emails.get(stranger.id)
    except NotFound:
        pass
    else:  # pragma: no cover
        raise AssertionError("one project read another's message")
