"""``POST /v1/emails`` and ``GET /v1/emails/{id}`` (§11, §12, §23).

The API's contract: validate what a caller can fix, record the message, hand it
to the queue, answer immediately. Whether the worker then sends it correctly is
a separate question with its own tests - here the queue is a recorder, so
"was a send actually queued?" is something these tests can assert rather than
infer.
"""

from __future__ import annotations

import base64
from typing import Any

from fakes.queue import FakeQueue
from fakes.ses import FakeProviderFactory
from httpx import AsyncClient
from seskit_core.models import EmailStatus
from seskit_core.services import (
    add_identity,
    connect_aws,
    create_api_key,
    create_project,
    register_user,
)
from seskit_core.services.identities import check_identity
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
EMAILS_URL = "/v1/emails"
REGION = "us-east-1"
SENDER = "Acme <hello@example.com>"

BODY: dict[str, Any] = {
    "from": SENDER,
    "to": ["user@example.com"],
    "subject": "Welcome to Acme",
    "html": "<h1>Welcome!</h1>",
    "text": "Welcome to Acme",
}


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _key(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    issued = await create_api_key(session, project_id=project.id, name="prod")
    return issued.raw_key


# ------------------------------------------------------------- accepting ---


async def test_a_send_is_accepted_and_queued(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """§11's documented response, and the job that makes it true."""
    raw_key = await _key(db_session)

    response = await app_client.post(EMAILS_URL, json=BODY, headers=_auth(raw_key))

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("email_")
    assert body["status"] == EmailStatus.QUEUED.value
    assert queue.ids_for("send_email") == [body["id"]]


async def test_a_single_recipient_may_be_a_string(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """§27 asks us to stay conceptually compatible with Resend, which takes
    either. Refusing the string form is a confusing 422 on someone's first call.
    """
    raw_key = await _key(db_session)

    response = await app_client.post(
        EMAILS_URL, json={**BODY, "to": "user@example.com"}, headers=_auth(raw_key)
    )

    assert response.status_code == 201


async def test_the_message_is_recorded(app_client: AsyncClient, db_session: AsyncSession) -> None:
    raw_key = await _key(db_session)
    created = (await app_client.post(EMAILS_URL, json=BODY, headers=_auth(raw_key))).json()

    stored = (await app_client.get(f"{EMAILS_URL}/{created['id']}", headers=_auth(raw_key))).json()

    assert stored["subject"] == "Welcome to Acme"
    assert stored["to"] == ["user@example.com"]
    assert stored["html"] == "<h1>Welcome!</h1>"


async def test_a_blind_copy_is_not_readable_back(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It is recorded - support gets asked - but a blind copy readable from the
    API is not blind.
    """
    raw_key = await _key(db_session)
    created = (
        await app_client.post(
            EMAILS_URL, json={**BODY, "bcc": ["quiet@example.com"]}, headers=_auth(raw_key)
        )
    ).json()

    stored = await app_client.get(f"{EMAILS_URL}/{created['id']}", headers=_auth(raw_key))

    assert "quiet@example.com" not in stored.text


# ----------------------------------------------------------- validation ---


async def test_an_unverified_sender_on_a_connected_project_is_refused(
    app_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: object,
    provider_factory: FakeProviderFactory,
    queue: FakeQueue,
) -> None:
    """The silent-failure guard, over HTTP: refused rather than quietly
    delivered to a local mailbox nobody reads.
    """
    user = await register_user(
        db_session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(db_session, user_id=user.id, name="Sending")
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project.id,
        region=REGION,
    )
    issued = await create_api_key(db_session, project_id=project.id, name="prod")

    response = await app_client.post(EMAILS_URL, json=BODY, headers=_auth(issued.raw_key))

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "domain_not_verified"
    assert queue.jobs == []


async def test_a_verified_sender_goes_through(
    app_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: object,
    provider_factory: FakeProviderFactory,
) -> None:
    user = await register_user(
        db_session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(db_session, user_id=user.id, name="Sending")
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project.id,
        region=REGION,
    )
    identity = await add_identity(
        db_session, provider_factory, project_id=project.id, value="example.com", region=REGION
    )
    provider_factory.provider.mark_verified("example.com")
    await check_identity(db_session, provider_factory, identity)
    issued = await create_api_key(db_session, project_id=project.id, name="prod")

    response = await app_client.post(EMAILS_URL, json=BODY, headers=_auth(issued.raw_key))

    assert response.status_code == 201


async def test_a_message_with_no_body_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    raw_key = await _key(db_session)
    payload = {key: value for key, value in BODY.items() if key not in {"html", "text"}}

    response = await app_client.post(EMAILS_URL, json=payload, headers=_auth(raw_key))

    assert response.status_code == 400
    assert queue.jobs == []


async def test_an_injected_header_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """Caught at the boundary, so it never reaches a provider or a queue."""
    raw_key = await _key(db_session)

    response = await app_client.post(
        EMAILS_URL,
        json={**BODY, "subject": "Hi\r\nBcc: quiet@evil.example"},
        headers=_auth(raw_key),
    )

    assert response.status_code == 400
    assert queue.jobs == []


async def test_an_oversized_message_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """§11's ceiling, enforced here rather than passed through as a raw SES
    rejection an hour later.
    """
    raw_key = await _key(db_session)
    huge = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()

    response = await app_client.post(
        EMAILS_URL,
        json={**BODY, "attachments": [{"filename": "big.bin", "content": huge}]},
        headers=_auth(raw_key),
    )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "attachment_too_large"
    assert queue.jobs == []


async def test_an_attachment_is_stored_for_the_worker(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A queued send has no request to read them from later."""
    from seskit_core.models import EmailAttachment
    from sqlalchemy import select

    raw_key = await _key(db_session)
    content = base64.b64encode(b"a,b\n1,2\n").decode()

    response = await app_client.post(
        EMAILS_URL,
        json={
            **BODY,
            "attachments": [
                {"filename": "report.csv", "content": content, "content_type": "text/csv"}
            ],
        },
        headers=_auth(raw_key),
    )

    assert response.status_code == 201
    stored = await db_session.scalar(select(EmailAttachment))
    assert stored is not None
    assert stored.content == b"a,b\n1,2\n"
    assert stored.size_bytes == 8


async def test_custom_headers_are_stored_for_the_worker(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """For the same reason attachments are, and for a while they were not.

    §11 accepts a `headers` object and this endpoint validated it against
    header injection - then had nowhere to put it, so the caller was told 201
    and nothing was sent. A queued message is assembled from the row, so the
    row is the only place a header can survive the response.
    """
    from seskit_core.models import Email
    from sqlalchemy import select

    raw_key = await _key(db_session)

    response = await app_client.post(
        EMAILS_URL,
        json={**BODY, "headers": {"X-Entity-Ref-Id": "order-1234"}},
        headers=_auth(raw_key),
    )

    assert response.status_code == 201
    stored = await db_session.scalar(select(Email))
    assert stored is not None
    assert stored.headers == {"X-Entity-Ref-Id": "order-1234"}


async def test_a_send_without_headers_stores_an_empty_object(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Not NULL. The column says what the message was sent with, and "we did
    not record them" is a different claim from "there were none".
    """
    from seskit_core.models import Email
    from sqlalchemy import select

    raw_key = await _key(db_session)

    await app_client.post(EMAILS_URL, json=BODY, headers=_auth(raw_key))

    stored = await db_session.scalar(select(Email))
    assert stored is not None
    assert stored.headers == {}


# ---------------------------------------------------------- idempotency ---


async def test_the_same_key_returns_the_first_result(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """§12: the existing record, not a second message."""
    raw_key = await _key(db_session)
    headers = {**_auth(raw_key), "Idempotency-Key": "order-42"}

    first = await app_client.post(EMAILS_URL, json=BODY, headers=headers)
    second = await app_client.post(EMAILS_URL, json=BODY, headers=headers)

    assert first.json()["id"] == second.json()["id"]
    assert len(queue.jobs) == 1


async def test_a_different_key_sends_again(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    raw_key = await _key(db_session)

    first = await app_client.post(
        EMAILS_URL, json=BODY, headers={**_auth(raw_key), "Idempotency-Key": "a"}
    )
    second = await app_client.post(
        EMAILS_URL, json=BODY, headers={**_auth(raw_key), "Idempotency-Key": "b"}
    )

    assert first.json()["id"] != second.json()["id"]
    assert len(queue.jobs) == 2


async def test_without_a_key_every_request_sends(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """The header is optional, and two identical sends without one are two
    deliberate sends.
    """
    raw_key = await _key(db_session)

    await app_client.post(EMAILS_URL, json=BODY, headers=_auth(raw_key))
    await app_client.post(EMAILS_URL, json=BODY, headers=_auth(raw_key))

    assert len(queue.jobs) == 2


# ------------------------------------------------------------ boundaries ---


async def test_a_key_cannot_read_another_projects_email(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A 404 rather than a 403 - the latter would confirm the id exists."""
    mine = await _key(db_session, email="me@example.com")
    theirs = await _key(db_session, email="them@example.com")
    created = (await app_client.post(EMAILS_URL, json=BODY, headers=_auth(mine))).json()

    response = await app_client.get(f"{EMAILS_URL}/{created['id']}", headers=_auth(theirs))

    assert response.status_code == 404


async def test_sending_needs_a_key(app_client: AsyncClient) -> None:
    response = await app_client.post(EMAILS_URL, json=BODY, follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_failed"


async def test_the_endpoints_are_documented(app_client: AsyncClient) -> None:
    schema = (await app_client.get("/openapi.json")).json()

    assert EMAILS_URL in schema["paths"]
    assert "/v1/emails/{email_id}" in schema["paths"]
    # §11's wire format, which the SDK will be generated against.
    body_schema = schema["components"]["schemas"]["SendEmailRequest"]
    assert "from" in body_schema["properties"]
    assert "sender" not in body_schema["properties"]
