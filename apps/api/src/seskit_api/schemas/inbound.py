"""Response models for received mail (inbound email).

Two shapes. The list returns a *summary* - who, what, when, how big - and no
bodies, because a page of a hundred messages with their HTML in it is a response
nobody asked for. The single message returns everything that was parsed.

Both are built by hand from the stored row rather than with ``from_attributes``.
The row stores things in shapes that suit the database - headers as pairs, the
envelope recipients under a name that only makes sense beside ``to_addresses`` -
and the public shape is a contract that should not move when the table does.

``from`` is a Python keyword, so the field is ``from_address`` with a
serialization alias, exactly as on ``EmailResponse``.

Everything in here was written by somebody else. Nothing is trusted, and the
descriptions say so where a consumer might otherwise assume it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from seskit_core.models import InboundEmail
from seskit_core.models.base import utcnow


class InboundVerdicts(BaseModel):
    """What SES concluded about the message. Recorded, never acted on."""

    spf: str | None = Field(
        default=None,
        description=(
            "`PASS`, `FAIL`, `GRAY` or `PROCESSING_FAILED`. Null means SES did not "
            "say, which is not the same as `PASS`."
        ),
        examples=["PASS"],
    )
    dkim: str | None = Field(
        default=None,
        description=(
            "The DKIM verdict, in the same values as `spf`. `GRAY` means the message "
            "was not signed or the signing domain does not match the sender's."
        ),
        examples=["GRAY"],
    )
    dmarc: str | None = Field(
        default=None, description="The DMARC verdict, in the same values as `spf`."
    )
    dmarc_policy: str | None = Field(
        default=None,
        description=(
            "The sender domain's published policy - `none`, `quarantine` or `reject` - "
            "and present only when DMARC failed."
        ),
    )
    spam: str | None = Field(default=None, description="The spam verdict, in the same values.")
    virus: str | None = Field(default=None, description="The virus verdict, in the same values.")


class InboundAttachment(BaseModel):
    """One attachment, described. The bytes are fetched separately."""

    index: int = Field(
        description=(
            "Its position among the message's attachments. Pass it to "
            "`GET /v1/inbound/{id}/attachments/{index}`."
        ),
        examples=[0],
    )
    filename: str = Field(
        description=(
            "Reduced to a plain name: no path, no control characters. The sender "
            "chose it, so treat it as untrusted text."
        ),
        examples=["report.pdf"],
    )
    content_type: str = Field(
        description=(
            "What the sender *declared*. It is not checked, and a download is always "
            "served as `application/octet-stream` regardless."
        ),
        examples=["application/pdf"],
    )
    size: int = Field(description="Size in bytes, after decoding.", examples=[48213])
    inline: bool = Field(
        description="Whether it is part of the message's layout, such as an embedded image."
    )
    content_id: str | None = Field(
        default=None,
        description="The `Content-ID` an HTML body refers to as `cid:`, if it has one.",
    )


class InboundHeader(BaseModel):
    """One header as the sender wrote it."""

    name: str = Field(description="The header name.", examples=["Received-SPF"])
    value: str = Field(
        description="The raw value, unfolded and not decoded. Untrusted.",
        examples=["pass"],
    )


class InboundSummary(BaseModel):
    """A received message, without its body. What the list returns."""

    id: str = Field(
        description="Opaque and stable, prefixed `inbound_`.",
        examples=["inbound_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    domain: str = Field(
        description="The domain the message was received for.", examples=["example.com"]
    )
    from_address: str = Field(
        serialization_alias="from",
        description=(
            "The address in the `From` header, lower-cased. Empty if there was none. "
            "Anybody can write anything here: see `verdicts` for what SES checked."
        ),
        examples=["ada@other.org"],
    )
    from_name: str = Field(description="The display name in `From`, if any.", examples=["Ada"])
    to: list[str] = Field(description="The addresses in the `To` header.")
    cc: list[str] = Field(description="The addresses in the `Cc` header. Empty if none.")
    subject: str = Field(description="The subject, decoded and on one line.")
    received_at: datetime = Field(description="When SES received the message. UTC.")
    size: int = Field(description="Size of the whole message in bytes.", examples=[4821])
    attachment_count: int = Field(description="How many attachments it has.", examples=[0])
    verdicts: InboundVerdicts = Field(description="What SES concluded about the message.")
    truncated: bool = Field(
        description=(
            "Whether a limit was hit while reading it, so something is missing from "
            "this representation. The original has all of it."
        )
    )
    parse_failed: bool = Field(
        description=(
            "Whether the message could not be read at all. Everything else is empty; "
            "the original is available through `/raw` until it expires."
        )
    )
    raw_available: bool = Field(
        description=(
            "Whether the original is expected to still be in storage. Retention "
            "removes it at a day boundary, so a message just past `raw_expires_at` "
            "may already be gone and one just before it should not be."
        )
    )
    raw_expires_at: datetime | None = Field(
        default=None,
        description=(
            "When retention will have removed the original and its attachments. "
            "Null when unknown. The parsed message below stays regardless. UTC."
        ),
    )


class InboundResponse(InboundSummary):
    """One received message, in full."""

    text_body: str = Field(
        serialization_alias="text",
        description="The plain-text body. Empty when the message had none.",
    )
    html_body: str = Field(
        serialization_alias="html",
        description=(
            "The HTML body, exactly as sent. **Untrusted.** It may contain script, "
            "tracking images and links that lie; do not render it in a page that "
            "carries your own credentials."
        ),
    )
    envelope_from: str = Field(
        description=("The SMTP envelope sender. Empty for a bounce. It need not match `from`."),
    )
    recipients: list[str] = Field(
        description=(
            "Who the message was addressed to on the wire - the addresses that "
            "matched your rule. A blind copy is here and in neither `to` nor `cc`."
        )
    )
    reply_to: list[str] = Field(description="The addresses in the `Reply-To` header.")
    message_id: str = Field(
        description="The `Message-ID` header, without angle brackets. Empty if none.",
    )
    in_reply_to: str = Field(
        description="The message this one answers, without angle brackets. Empty if none.",
    )
    references: list[str] = Field(
        description="The ids of the thread it belongs to, oldest first, without brackets."
    )
    attachments: list[InboundAttachment] = Field(
        description="Described in the order `/attachments/{index}` counts them."
    )
    headers: list[InboundHeader] = Field(
        description="Every header as sent, in order, up to a limit. See `truncated`."
    )


class InboundList(BaseModel):
    """One page of a project's received messages, newest first."""

    data: list[InboundSummary] = Field(
        description="The page, ordered by id descending - newest first."
    )
    has_more: bool = Field(
        description=(
            "Whether another page exists. Pass the last id in `data` as "
            "`starting_after` to fetch it."
        ),
        examples=[False],
    )


def _raw_available(row: InboundEmail) -> bool:
    return row.raw_expires_at is None or row.raw_expires_at > utcnow()


def summary_of(row: InboundEmail) -> InboundSummary:
    return InboundSummary(
        id=row.id,
        domain=row.domain,
        from_address=row.from_address,
        from_name=row.from_name,
        to=row.to_addresses,
        cc=row.cc_addresses,
        subject=row.subject,
        received_at=row.received_at,
        size=row.size_bytes,
        attachment_count=len(row.attachments),
        verdicts=InboundVerdicts(
            spf=row.spf_verdict,
            dkim=row.dkim_verdict,
            dmarc=row.dmarc_verdict,
            dmarc_policy=row.dmarc_policy,
            spam=row.spam_verdict,
            virus=row.virus_verdict,
        ),
        truncated=row.truncated,
        parse_failed=row.parse_failed,
        raw_available=_raw_available(row),
        raw_expires_at=row.raw_expires_at,
    )


def detail_of(row: InboundEmail) -> InboundResponse:
    return InboundResponse(
        **summary_of(row).model_dump(),
        text_body=row.text_body,
        html_body=row.html_body,
        envelope_from=row.envelope_from,
        recipients=row.envelope_to,
        reply_to=row.reply_to,
        message_id=row.message_id_header,
        in_reply_to=row.in_reply_to,
        references=row.reference_ids,
        attachments=[InboundAttachment(**attachment) for attachment in row.attachments],
        headers=[InboundHeader(name=name, value=value) for name, value in row.headers],
    )
