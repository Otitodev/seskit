"""Sending through SES.

moto does not usefully mock SendEmail, so a fake boto3 client stands in and what
is checked is the request we build - which is where the decisions live: when SES
is given fields and when it is given assembled MIME, and whether blind copies
reach the envelope.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers.types import Attachment, OutboundEmail
from seskit_provider_aws_ses import SESProvider

REGION = "us-east-1"
MESSAGE_ID = "0100018f-aaaa-bbbb-cccc-000000000000"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "SendEmail")


class FakeSESClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.request: dict[str, Any] = {}

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.request = kwargs
        return {"MessageId": MESSAGE_ID}


def _provider(monkeypatch: pytest.MonkeyPatch, client: FakeSESClient) -> SESProvider:
    provider = SESProvider(REGION)
    monkeypatch.setattr(provider._session, "client", lambda *a, **kw: client)
    return provider


def _email(**kwargs: Any) -> OutboundEmail:
    defaults: dict[str, Any] = {
        "sender": "hello@example.com",
        "to": ["user@example.com"],
        "subject": "Welcome",
        "text": "Hello",
        "html": "<h1>Hello</h1>",
    }
    defaults.update(kwargs)
    return OutboundEmail(**defaults)


# --------------------------------------------------------------- success ---


async def test_the_provider_message_id_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 7 joins every incoming SES notification back to a row on this."""
    client = FakeSESClient()

    result = await _provider(monkeypatch, client).send_email(_email())

    assert result.provider_message_id == MESSAGE_ID


async def test_a_plain_message_is_sent_as_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simple content lets SES assemble the message, which it handles better
    than being handed a blob.
    """
    client = FakeSESClient()

    await _provider(monkeypatch, client).send_email(_email())

    content = client.request["Content"]
    assert "Simple" in content
    assert content["Simple"]["Subject"]["Data"] == "Welcome"
    assert content["Simple"]["Body"]["Text"]["Data"] == "Hello"
    assert content["Simple"]["Body"]["Html"]["Data"] == "<h1>Hello</h1>"


async def test_recipients_go_in_the_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient()

    await _provider(monkeypatch, client).send_email(
        _email(cc=["cc@example.com"], bcc=["quiet@example.com"], reply_to=["r@example.com"])
    )

    destination = client.request["Destination"]
    assert destination["ToAddresses"] == ["user@example.com"]
    assert destination["CcAddresses"] == ["cc@example.com"]
    assert destination["BccAddresses"] == ["quiet@example.com"]
    assert client.request["ReplyToAddresses"] == ["r@example.com"]


# ------------------------------------------------------------------- raw ---


async def test_an_attachment_forces_raw_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simple content has no field for an attachment, so it would be dropped
    silently - the message would send, without the file.
    """
    client = FakeSESClient()
    attachment = Attachment(filename="a.csv", content=b"x,y\n", content_type="text/csv")

    await _provider(monkeypatch, client).send_email(_email(attachments=[attachment]))

    content = client.request["Content"]
    assert "Raw" in content
    assert b"a.csv" in content["Raw"]["Data"]


async def test_a_custom_header_also_forces_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reasoning: Simple content can only express what its own fields
    cover, and there is no field for an arbitrary header.
    """
    client = FakeSESClient()

    await _provider(monkeypatch, client).send_email(_email(headers={"X-Campaign": "spring"}))

    assert b"X-Campaign: spring" in client.request["Content"]["Raw"]["Data"]


async def test_raw_still_passes_the_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise SES takes recipients from the headers, and a blind copy is
    deliberately not in the headers - it would simply never be delivered.
    """
    client = FakeSESClient()
    attachment = Attachment(filename="a.txt", content=b"x")

    await _provider(monkeypatch, client).send_email(
        _email(bcc=["quiet@example.com"], attachments=[attachment])
    )

    assert client.request["Destination"]["BccAddresses"] == ["quiet@example.com"]
    assert b"quiet@example.com" not in client.request["Content"]["Raw"]["Data"]


# ---------------------------------------------------------------- errors ---


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("MessageRejected", ErrorType.EMAIL_REJECTED),
        ("MailFromDomainNotVerifiedException", ErrorType.EMAIL_REJECTED),
        ("AccountSuspendedException", ErrorType.SENDING_LIMIT_EXCEEDED),
        ("SendingPausedException", ErrorType.SENDING_LIMIT_EXCEEDED),
        ("LimitExceededException", ErrorType.SENDING_LIMIT_EXCEEDED),
        ("ThrottlingException", ErrorType.PROVIDER_ERROR),
        ("AccessDeniedException", ErrorType.AUTHORIZATION_FAILED),
    ],
)
async def test_send_failures_are_normalised(
    monkeypatch: pytest.MonkeyPatch, code: str, expected: ErrorType
) -> None:
    client = FakeSESClient(error=_client_error(code))

    with pytest.raises(APIError) as caught:
        await _provider(monkeypatch, client).send_email(_email())

    assert caught.value.error_type is expected


async def test_a_rejection_explains_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """By far the most common reason a first real send fails, and the message
    SES returns does not say so.
    """
    client = FakeSESClient(error=_client_error("MessageRejected"))

    with pytest.raises(APIError) as caught:
        await _provider(monkeypatch, client).send_email(_email())

    assert "sandbox" in caught.value.message.lower()


async def test_a_denied_send_names_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient(error=_client_error("AccessDeniedException"))

    with pytest.raises(APIError) as caught:
        await _provider(monkeypatch, client).send_email(_email())

    assert "ses:SendEmail" in caught.value.message
