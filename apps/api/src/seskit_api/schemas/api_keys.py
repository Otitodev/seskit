"""API key representations.

The raw key is absent from every one of these by construction. It exists once,
in the dashboard's create response, and is never part of a read.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class APIKeyResponse(BaseModel):
    """A key as the API describes it - prefix only, never the secret."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(examples=["key_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    name: str = Field(examples=["production"])
    key_prefix: str = Field(
        description="The first characters of the key, for recognising it in a list.",
        examples=["sk_3nK9vQ2m"],
    )
    created_at: datetime
    last_used_at: datetime | None = Field(
        default=None,
        description="Accurate to the minute; not updated on every request.",
    )
    revoked_at: datetime | None = Field(
        default=None, description="Null while the key is active. Revocation is permanent."
    )


class APIKeyList(BaseModel):
    data: list[APIKeyResponse]
