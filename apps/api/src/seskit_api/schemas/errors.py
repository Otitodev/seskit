"""The §19 error envelope, as a schema.

Declared so the shape appears in the OpenAPI document. A customer generating a
client should find the error type in the spec rather than by triggering one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    type: str = Field(
        description="Machine-readable error type.", examples=["authentication_failed"]
    )
    message: str = Field(
        description="Human-readable explanation. Not intended for pattern matching.",
        examples=["Invalid or missing API key."],
    )


class ErrorResponse(BaseModel):
    error: ErrorBody = Field(
        description="Every error from this API has this shape, whatever the status code."
    )
