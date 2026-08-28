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

    filename: str = Field(examples=["report.csv"], max_length=255)
    content: str = Field(description="Base64-encoded file content.")
    content_type: str = Field(default="application/octet-stream", examples=["text/csv"])

    def decoded(self) -> bytes:
        try:
            return base64.b64decode(self.content, validate=True)
        except (binascii.Error, ValueError) as exc:  # pragma: no cover - trivial
            raise ValueError(f"{self.filename} is not valid base64.") from exc


class SendEmailRequest(BaseModel):
    """§11's request body."""

    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(alias="from", examples=["Acme <hello@example.com>"], max_length=320)
    to: Recipients
    subject: str = Field(examples=["Welcome to Acme"], max_length=998)
    html: str | None = Field(default=None, examples=["<h1>Welcome!</h1>"])
    text: str | None = Field(default=None, examples=["Welcome to Acme"])
    cc: Recipients | None = None
    bcc: Recipients | None = None
    reply_to: Recipients | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[AttachmentRequest] = Field(default_factory=list)

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

    id: str = Field(examples=["email_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    status: str = Field(examples=["queued"])


class EmailResponse(BaseModel):
    """A stored message, as a customer sees it.

    Bcc is deliberately absent. It is recorded - support gets asked - but a
    blind copy readable from the API is not blind.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str = Field(examples=["sent"])
    from_address: str = Field(serialization_alias="from", examples=["hello@example.com"])
    to_addresses: list[str] = Field(serialization_alias="to")
    cc_addresses: list[str] = Field(serialization_alias="cc")
    reply_to: list[str]
    subject: str
    html_body: str | None = Field(default=None, serialization_alias="html")
    text_body: str | None = Field(default=None, serialization_alias="text")
    provider_message_id: str | None = None
    last_error: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None


class EmailList(BaseModel):
    data: list[EmailResponse]
