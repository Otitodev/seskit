"""The SMTP provider, against a real Mailpit.

§25 asks for exactly this: Mailpit's REST API lets a test assert on what
actually arrived, so the provider is exercised against a real SMTP server rather
than a mock of one. A fake would happily accept a message whose blind copies
were in the headers.

Requires the compose stack. Skipped, not failed, when Mailpit is not up - the
same courtesy the Postgres fixtures extend.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import EmailProvider
from seskit_core.providers.types import Attachment, OutboundEmail
from seskit_provider_smtp import SMTPProvider, SMTPSettings

MAILPIT_API = "http://localhost:8025/api/v1"
MAILPIT_SMTP_HOST = "localhost"
MAILPIT_SMTP_PORT = 1025

SENDER = "SESKit Tests <tests@seskit.local>"


def _settings(**kwargs: Any) -> SMTPSettings:
    defaults: dict[str, Any] = {"host": MAILPIT_SMTP_HOST, "port": MAILPIT_SMTP_PORT}
    defaults.update(kwargs)
    return SMTPSettings(**defaults)


def _email(**kwargs: Any) -> OutboundEmail:
    defaults: dict[str, Any] = {
        "sender": SENDER,
        "to": ["user@example.com"],
        "subject": "Welcome",
        "text": "Hello there",
        "html": "<h1>Hello there</h1>",
    }
    defaults.update(kwargs)
    return OutboundEmail(**defaults)


@pytest.fixture
async def mailpit() -> Any:
    """A clean Mailpit inbox, or a skip if it is not running."""
    async with httpx.AsyncClient(base_url=MAILPIT_API, timeout=5.0) as client:
        try:
            await client.get("/messages")
        except httpx.HTTPError:
            pytest.skip("Mailpit is not running - start the compose stack")
        await client.delete("/messages")
        yield client


async def _latest(mailpit: httpx.AsyncClient) -> dict[str, Any]:
    listing = (await mailpit.get("/messages")).json()
    assert listing["messages"], "Mailpit received nothing"
    message_id = listing["messages"][0]["ID"]
    detail: dict[str, Any] = (await mailpit.get(f"/message/{message_id}")).json()
    return detail


# ------------------------------------------------------------------ shape ---


def test_the_smtp_provider_satisfies_the_interface() -> None:
    assert isinstance(SMTPProvider(_settings()), EmailProvider)


async def test_it_reports_no_sandbox() -> None:
    """The sandbox is an SES concept. Claiming one here would put a warning on
    screen about a limit that does not exist locally.
    """
    status = await SMTPProvider(_settings()).verify_account()

    assert status.sandbox is False
    assert status.sending_enabled is True


async def test_every_identity_is_already_verified() -> None:
    """A local relay accepts whatever it is given, so inventing a restriction
    would block the very thing this provider exists to allow.
    """
    status = await SMTPProvider(_settings()).get_identity_status("anything.example")

    assert status.is_verified is True


# ------------------------------------------------------- delivery to Mailpit ---


async def test_a_message_arrives_with_its_subject_and_body(mailpit: httpx.AsyncClient) -> None:
    """The headline claim of the whole phase: a send that needs no AWS."""
    result = await SMTPProvider(_settings()).send_email(_email())

    message = await _latest(mailpit)
    assert message["Subject"] == "Welcome"
    assert "Hello there" in message["Text"]
    assert result.provider_message_id != ""


async def test_the_html_alternative_arrives_too(mailpit: httpx.AsyncClient) -> None:
    await SMTPProvider(_settings()).send_email(_email())

    message = await _latest(mailpit)
    assert "<h1>Hello there</h1>" in message["HTML"]


async def test_recipients_land_where_they_belong(mailpit: httpx.AsyncClient) -> None:
    await SMTPProvider(_settings()).send_email(
        _email(to=["a@example.com"], cc=["b@example.com"], reply_to=["reply@example.com"])
    )

    message = await _latest(mailpit)
    assert [item["Address"] for item in message["To"]] == ["a@example.com"]
    assert [item["Address"] for item in message["Cc"]] == ["b@example.com"]
    assert [item["Address"] for item in message["ReplyTo"]] == ["reply@example.com"]


async def test_a_blind_copy_is_delivered_but_not_disclosed(
    mailpit: httpx.AsyncClient,
) -> None:
    """The test a fake provider could not do honestly.

    Mailpit adds a ``Bcc`` header of its own for any envelope recipient that was
    not in the visible headers - the same thing a receiving MTA does, alongside
    Return-Path and Received. So its presence here is the evidence, not a
    failure: it means the address travelled in the envelope rather than in the
    message. That the message *we* built carries no Bcc is asserted in
    ``test_mime.py``, where it can be checked before anything touches a wire.
    """
    await SMTPProvider(_settings()).send_email(
        _email(to=["a@example.com"], bcc=["quiet@example.com"])
    )

    message = await _latest(mailpit)
    recipients = {item["Address"] for item in message["To"]} | {
        item["Address"] for item in message.get("Bcc") or []
    }

    assert "quiet@example.com" in recipients
    # And it is not a visible recipient of the message.
    assert "quiet@example.com" not in {item["Address"] for item in message["To"]}


async def test_an_attachment_arrives(mailpit: httpx.AsyncClient) -> None:
    attachment = Attachment(filename="report.csv", content=b"a,b\n1,2\n", content_type="text/csv")

    await SMTPProvider(_settings()).send_email(_email(attachments=[attachment]))

    message = await _latest(mailpit)
    names = [item["FileName"] for item in message["Attachments"]]
    assert names == ["report.csv"]


async def test_a_custom_header_arrives(mailpit: httpx.AsyncClient) -> None:
    await SMTPProvider(_settings()).send_email(_email(headers={"X-Campaign": "spring"}))

    message = await _latest(mailpit)
    raw = (await mailpit.get(f"/message/{message['ID']}/raw")).text
    assert "X-Campaign: spring" in raw


# ----------------------------------------------------------------- errors ---


async def test_an_unreachable_server_is_a_provider_error() -> None:
    """Retryable, and normalised - a socket error must not reach a caller as a
    traceback.
    """
    provider = SMTPProvider(_settings(port=1, timeout=2.0))

    with pytest.raises(APIError) as caught:
        await provider.send_email(_email())

    assert caught.value.error_type is ErrorType.PROVIDER_ERROR


async def test_an_invalid_address_is_refused_before_connecting() -> None:
    """Assembly rejects it, so a malformed recipient never costs a round trip."""
    provider = SMTPProvider(_settings())

    with pytest.raises(APIError) as caught:
        await provider.send_email(_email(to=["not-an-address"]))

    assert caught.value.error_type is ErrorType.INVALID_RECIPIENT
