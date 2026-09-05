"""Request and response models for sending (§11, §23).

The wire format is §11's, which is also what the SDK will be generated against,
so the field names here are a contract rather than an implementation detail.

``from`` is a Python keyword, so the field is ``sender`` with ``alias="from"``.
The alias is what customers see and what the OpenAPI document carries; the
Python name never leaves this process.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A single recipient or a list of them. §11 documents a list, and Resend - which
#: §27 asks us to stay conceptually compatible with - accepts either. Taking
#: both costs one validator and saves a confusing 422 on someone's first call.
Recipients = Annotated[list[str] | str, Field(examples=[["user@example.com"]])]


def _as_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


class AttachmentRequest(BaseModel):
    """One attachment, base64 encoded.

    JSON has no byte type, so content arrives encoded. The size limit is applied
    to the *assembled message* rather than to this field, because base64 inflates
    content by about a third and it is the assembled size SES rejects.
    """

    filename: str = Field(
        description="The name the recipient sees. Max 255 characters.",
        examples=["report.csv"],
        max_length=255,
    )
    content: str = Field(description="Base64-encoded file content.")
    content_type: str = Field(
        default="application/octet-stream",
        description=(
            "MIME type. Defaults to `application/octet-stream`, which most clients "
            "offer as a download rather than displaying."
        ),
        examples=["text/csv"],
    )

    def decoded(self) -> bytes:
        try:
            return base64.b64decode(self.content, validate=True)
        except (binascii.Error, ValueError) as exc:  # pragma: no cover - trivial
            raise ValueError(f"{self.filename} is not valid base64.") from exc


class SendEmailRequest(BaseModel):
    """§11's request body."""

    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(
        alias="from",
        description=(
            "The sender, optionally with a display name. The address or its domain "
            "must be verified in SES — an unverified sender is refused by SES, not "
            "by SESKit, so it fails at send time rather than here."
        ),
        examples=["Acme <hello@example.com>"],
        max_length=320,
    )
    to: Recipients = Field(
        description=(
            "One recipient, or a list of them. While your account is in the SES "
            "sandbox every recipient must also be verified."
        ),
    )
    subject: str = Field(
        description="Max 998 characters, which is the RFC 5322 line limit.",
        examples=["Welcome to Acme"],
        max_length=998,
    )
    html: str | None = Field(
        default=None,
        description="HTML body. Provide `html`, `text`, or both; a message with neither is refused.",
        examples=["<h1>Welcome!</h1>"],
    )
    text: str | None = Field(
        default=None,
        description=(
            "Plain-text body. Sending both makes a multipart message, which is what "
            "clients that will not render HTML fall back to."
        ),
        examples=["Welcome to Acme"],
    )
    cc: Recipients | None = Field(default=None, description="Visible to every recipient.")
    bcc: Recipients | None = Field(
        default=None,
        description=(
            "Hidden from every recipient. Recorded, but never returned by the API — "
            "a blind copy readable from a `GET` is not blind."
        ),
    )
    reply_to: Recipients | None = Field(
        default=None,
        description="Where replies go, if not to `from`. Needs no SES verification.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Custom headers to add to the message.",
        examples=[{"X-Entity-Ref-Id": "order-1234"}],
    )
    attachments: list[AttachmentRequest] = Field(
        default_factory=list,
        description=(
            "The size limit applies to the assembled message, not to each file: "
            "base64 inflates content by about a third, and it is the assembled size "
            "SES rejects. See `EMAIL_MAX_MESSAGE_BYTES` (10 MiB by default)."
        ),
    )

    @field_validator("to", "cc", "bcc", "reply_to", mode="after")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        return _as_list(value)

    @property
    def to_list(self) -> list[str]:
        return _as_list(self.to)

    @property
    def cc_list(self) -> list[str]:
        return _as_list(self.cc)

    @property
    def bcc_list(self) -> list[str]:
        return _as_list(self.bcc)

    @property
    def reply_to_list(self) -> list[str]:
        return _as_list(self.reply_to)


class SendEmailResponse(BaseModel):
    """§11's response: the id, and that it was accepted.

    ``queued`` rather than ``sent`` because that is what has happened. The
    message is durable and will be attempted; claiming it had gone out would be
    a promise this response cannot make.
    """

    id: str = Field(
        description="Use it to fetch the message later, and to match delivery events to it.",
        examples=["email_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    status: str = Field(
        description=(
            "Always `queued` here. The message is durable and will be attempted; "
            "whether it was accepted by SES is reported later, through events."
        ),
        examples=["queued"],
    )


class EmailResponse(BaseModel):
    """A stored message, as a customer sees it.

    Bcc is deliberately absent. It is recorded - support gets asked - but a
    blind copy readable from the API is not blind.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description="Opaque and stable, prefixed `email_`.",
        examples=["email_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    status: str = Field(
        description=(
            "`queued`, `sending`, `sent` or `failed`. Note that `sent` is the end of "
            "this vocabulary: it means a provider accepted the message, which is the "
            "last thing SESKit can observe by itself. Whether it *arrived* is a "
            "delivery event, so read `delivered_at` rather than expecting a "
            "`delivered` status."
        ),
        examples=["sent"],
    )
    from_address: str = Field(
        serialization_alias="from",
        description="As sent, display name included.",
        examples=["hello@example.com"],
    )
    to_addresses: list[str] = Field(
        serialization_alias="to", description="Always a list here, even if one address was sent."
    )
    cc_addresses: list[str] = Field(
        serialization_alias="cc",
        description="Empty when the message had none. Bcc is never returned.",
    )
    reply_to: list[str] = Field(description="Empty when the message did not set one.")
    subject: str = Field(description="As sent.")
    html_body: str | None = Field(
        default=None, serialization_alias="html", description="Null when the message was text only."
    )
    text_body: str | None = Field(
        default=None, serialization_alias="text", description="Null when the message was HTML only."
    )
    provider_message_id: str | None = Field(
        default=None,
        description=(
            "The id SES gave the message. Null until it is accepted. This is the id "
            "to quote to AWS support, and the one delivery events carry."
        ),
    )
    last_error: str | None = Field(
        default=None,
        description="Why the last attempt failed, normalised. Null unless `status` is `failed`.",
    )
    created_at: datetime = Field(description="When SESKit accepted the message. UTC.")
    sent_at: datetime | None = Field(
        default=None, description="When SES accepted it. Null while queued. UTC."
    )
    delivered_at: datetime | None = Field(
        default=None,
        description=(
            "When the receiving server accepted it. Null unless a delivery event has "
            "arrived, which needs event reporting to be set up. UTC."
        ),
    )


class EmailList(BaseModel):
    """Declared, but nothing returns it yet - there is no list-emails endpoint.

    Left in place because §11's shape is the contract the SDK will be generated
    against, and deliberately undescribed: an ordering documented here would be
    a promise made before the query that has to keep it exists.
    """

    data: list[EmailResponse]
