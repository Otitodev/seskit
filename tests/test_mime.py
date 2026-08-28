"""Assembling a MIME message.

Pure - no database, no provider, no Docker. These are the details that are
tedious to get right and invisible when wrong: a client showing plain text
because the alternatives were ordered backwards, a blind copy that was not
blind, a subject that carried a header into the message with it.
"""

from __future__ import annotations

import pytest
from seskit_core.email import (
    assert_within_size,
    build_message,
    envelope_recipients,
    message_bytes,
)
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers.types import Attachment, OutboundEmail

SENDER = "Acme <hello@example.com>"
TO = ["user@example.com"]


def _email(**kwargs: object) -> OutboundEmail:
    defaults: dict[str, object] = {
        "sender": SENDER,
        "to": list(TO),
        "subject": "Welcome",
        "html": "<h1>Hello</h1>",
        "text": "Hello",
    }
    defaults.update(kwargs)
    return OutboundEmail(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- headers ---


def test_a_display_name_survives() -> None:
    """§11's own example is "Acme <hello@example.com>", so this form has to
    work rather than being treated as a malformed address.
    """
    message = build_message(_email())

    assert "Acme" in message["From"]
    assert "hello@example.com" in message["From"]


def test_recipients_and_reply_to_land_in_their_headers() -> None:
    message = build_message(_email(cc=["cc@example.com"], reply_to=["reply@example.com"]))

    assert "user@example.com" in message["To"]
    assert "cc@example.com" in message["Cc"]
    assert "reply@example.com" in message["Reply-To"]


def test_custom_headers_are_carried() -> None:
    message = build_message(_email(headers={"X-Campaign": "spring"}))

    assert message["X-Campaign"] == "spring"


def test_a_custom_header_cannot_overwrite_the_sender() -> None:
    """Otherwise a caller sends as one address while the record says another,
    which makes every audit trail a fiction.
    """
    with pytest.raises(APIError) as caught:
        build_message(_email(headers={"From": "someone@evil.example"}))

    assert caught.value.error_type is ErrorType.INVALID_REQUEST


def test_a_custom_header_cannot_smuggle_in_a_bcc() -> None:
    with pytest.raises(APIError):
        build_message(_email(headers={"Bcc": "quiet@evil.example"}))


# -------------------------------------------------------------- injection ---


def test_a_newline_in_the_subject_is_refused() -> None:
    """Header injection. A subject of "Hi\\r\\nBcc: someone@evil" silently
    gaining a recipient is exactly the bug that must not exist.
    """
    with pytest.raises(APIError) as caught:
        build_message(_email(subject="Hi\r\nBcc: quiet@evil.example"))

    assert caught.value.error_type is ErrorType.INVALID_REQUEST


def test_a_newline_in_a_custom_header_is_refused() -> None:
    with pytest.raises(APIError):
        build_message(_email(headers={"X-Thing": "a\r\nBcc: quiet@evil.example"}))


def test_a_newline_in_a_filename_is_refused() -> None:
    attachment = Attachment(filename="ok.txt\r\nBcc: x@evil.example", content=b"hi")

    with pytest.raises(APIError):
        build_message(_email(attachments=[attachment]))


# ------------------------------------------------------------ body layout ---


def test_text_and_html_become_alternatives_in_the_right_order() -> None:
    """A client picks the *last* part it understands. Reversed, everyone sees
    plain text and the HTML is never rendered.
    """
    message = build_message(_email())

    assert message.get_content_type() == "multipart/alternative"
    subtypes = [part.get_content_subtype() for part in message.iter_parts()]
    assert subtypes == ["plain", "html"]


def test_html_only_is_sent_as_html() -> None:
    message = build_message(_email(text=None))

    assert message.get_content_type() == "text/html"


def test_text_only_is_sent_as_text() -> None:
    message = build_message(_email(html=None))

    assert message.get_content_type() == "text/plain"


def test_a_message_with_no_body_is_refused() -> None:
    with pytest.raises(APIError):
        build_message(_email(html=None, text=None))


def test_a_message_with_no_recipient_is_refused() -> None:
    with pytest.raises(APIError):
        build_message(_email(to=[]))


def test_an_invalid_address_is_refused_as_such() -> None:
    with pytest.raises(APIError) as caught:
        build_message(_email(to=["not-an-address"]))

    assert caught.value.error_type is ErrorType.INVALID_RECIPIENT


# ------------------------------------------------------------------- bcc ---


def test_bcc_never_appears_in_the_message() -> None:
    """The whole point of a blind copy. If it is in the assembled message every
    recipient can read it.
    """
    message = build_message(_email(bcc=["quiet@example.com"]))

    assert message["Bcc"] is None
    assert b"quiet@example.com" not in message.as_bytes()


def test_bcc_is_still_an_envelope_recipient() -> None:
    """It has to reach them - it just must not be written down."""
    recipients = envelope_recipients(_email(cc=["cc@example.com"], bcc=["quiet@example.com"]))

    assert set(recipients) == {"user@example.com", "cc@example.com", "quiet@example.com"}


def test_a_repeated_recipient_is_only_sent_once() -> None:
    recipients = envelope_recipients(_email(to=["a@example.com"], cc=["a@example.com"]))

    assert recipients == ["a@example.com"]


# ----------------------------------------------------------- attachments ---


def test_an_attachment_survives_assembly() -> None:
    attachment = Attachment(filename="report.csv", content=b"a,b\n1,2\n", content_type="text/csv")

    message = build_message(_email(attachments=[attachment]))

    parts = list(message.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "report.csv"
    assert parts[0].get_content_type() == "text/csv"
    assert parts[0].get_payload(decode=True) == b"a,b\n1,2\n"


def test_a_malformed_content_type_falls_back_rather_than_failing() -> None:
    """A bad content type is the caller being careless, not an attack. Sending
    it as a generic binary is more useful than refusing the whole message.
    """
    attachment = Attachment(filename="thing", content=b"x", content_type="nonsense")

    message = build_message(_email(attachments=[attachment]))

    assert next(message.iter_attachments()).get_content_type() == "application/octet-stream"


# ------------------------------------------------------------------ size ---


def test_a_small_message_passes_the_size_check() -> None:
    size = assert_within_size(_email(), max_bytes=10 * 1024 * 1024)

    assert 0 < size < 10_000


def test_an_oversized_message_is_refused_as_attachment_too_large() -> None:
    attachment = Attachment(filename="big.bin", content=b"x" * 200_000)

    with pytest.raises(APIError) as caught:
        assert_within_size(_email(attachments=[attachment]), max_bytes=100_000)

    assert caught.value.error_type is ErrorType.ATTACHMENT_TOO_LARGE


def test_the_size_checked_is_the_encoded_one() -> None:
    """Base64 inflates by about a third. Checking the raw sum would wave through
    a message the provider then rejects - which is the failure §11 asks us to
    catch at our own boundary.
    """
    raw = 90_000
    attachment = Attachment(filename="big.bin", content=b"x" * raw)

    size = len(message_bytes(_email(attachments=[attachment])))

    assert size > raw * 1.3
