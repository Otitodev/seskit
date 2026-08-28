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

    record_type: str = Field(examples=["CNAME"])
    name: str = Field(examples=["abc123._domainkey.example.com"])
    value: str = Field(examples=["abc123.dkim.amazonses.com"])


class DomainResponse(BaseModel):
    """A sending domain and where its verification has got to."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(examples=["dom_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    #: The domain itself. Named ``name`` rather than ``value`` because that is
    #: what it is to a caller reading the API.
    value: str = Field(examples=["example.com"])
    region: str = Field(examples=["us-east-1"])
    verification_status: str = Field(examples=["success"])
    dkim_status: str | None = Field(default=None, examples=["success"])
    mail_from_status: str | None = Field(default=None, examples=["not_started"])
    #: Built from the stored DKIM tokens, so a caller can render setup
    #: instructions without a second request.
    dns_records: list[DnsRecordResponse] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    created_at: datetime


class DomainList(BaseModel):
    data: list[DomainResponse]
