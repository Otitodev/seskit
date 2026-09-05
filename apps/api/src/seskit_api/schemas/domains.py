"""Response models for the public domains endpoint (§23).

What a customer sees, and deliberately not everything the row holds:
``last_error`` stays internal. It is normalised (§19) and safe in the dashboard,
but it describes *our* connection to AWS rather than anything the caller can act
on, and putting it in an API response would make it a contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DnsRecordResponse(BaseModel):
    """One record the customer has to publish.

    ``from_attributes`` because the source is a frozen dataclass, not a dict -
    pydantic will not read a nested dataclass by attribute without it.
    """

    model_config = ConfigDict(from_attributes=True)

    record_type: str = Field(
        description="`CNAME` — the three records SES Easy DKIM needs are all CNAMEs.",
        examples=["CNAME"],
    )
    name: str = Field(
        description=(
            "The fully-qualified record name. Some DNS providers want only the part "
            "before your domain, so check whether yours appends it for you."
        ),
        examples=["abc123._domainkey.example.com"],
    )
    value: str = Field(
        description="What the record should point at.",
        examples=["abc123.dkim.amazonses.com"],
    )


class DomainResponse(BaseModel):
    """A sending domain and where its verification has got to."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description="Opaque and stable, prefixed `dom_`.",
        examples=["dom_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    value: str = Field(description="The domain itself.", examples=["example.com"])
    region: str = Field(
        description=(
            "The AWS region the identity lives in. SES identities are per-region, so a "
            "domain verified in one region is not verified in another."
        ),
        examples=["us-east-1"],
    )
    #: The three status fields share SES's own vocabulary, kept verbatim so a
    #: status never has to be translated twice.
    verification_status: str = Field(
        description=(
            "`pending`, `success`, `failed`, `temporary_failure` or `not_started`. "
            "You can only send from the domain once this is `success`."
        ),
        examples=["success"],
    )
    dkim_status: str | None = Field(
        default=None,
        description=(
            "Same vocabulary. Mail sends without DKIM, but signing it is what stops "
            "receivers treating it as unauthenticated."
        ),
        examples=["success"],
    )
    mail_from_status: str | None = Field(
        default=None,
        description="Same vocabulary, for a custom MAIL FROM domain. Optional.",
        examples=["not_started"],
    )
    dns_records: list[DnsRecordResponse] = Field(
        default_factory=list,
        description=(
            "The records to publish, built from the stored DKIM tokens so you can render "
            "setup instructions without a second request."
        ),
    )
    last_checked_at: datetime | None = Field(
        default=None,
        description="When SESKit last asked SES about this domain. Null until first checked. UTC.",
    )
    created_at: datetime = Field(description="UTC.")


class DomainList(BaseModel):
    data: list[DomainResponse] = Field(description="Every domain on the project.")
