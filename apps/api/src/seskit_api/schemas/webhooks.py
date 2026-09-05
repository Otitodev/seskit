"""Response models for the public webhooks endpoints (§23).

**The signing secret is deliberately absent.** It is shown in full on the
dashboard, where a human reads it once while configuring their receiver, and
that is a different exposure from returning it in every list response an
application makes. An API key lives in application code and its responses end up
in logs, traces and error reports; a signing secret that travels that way is one
an attacker eventually reads without ever touching the dashboard. Nothing a
caller can do with the API needs it - verification happens at *their* endpoint,
with the copy they already configured.

``consecutive_failures`` is exposed, because an application watching its own
integration should be able to see it climbing before SESKit switches the
endpoint off.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WebhookEndpointResponse(BaseModel):
    """A registered destination and whether SESKit is still sending to it."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description="Opaque and stable, prefixed `wh_`.",
        examples=["wh_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    url: str = Field(
        description=(
            "Where SESKit POSTs events. Must be https, and must not resolve to a "
            "loopback, private or link-local address — delivery responses are shown "
            "in the dashboard, so an internal URL would turn a webhook into a read "
            "primitive against your own network. Local development relaxes both rules "
            "so you can point one at your own machine."
        ),
        examples=["https://example.com/webhooks/email"],
    )
    status: str = Field(
        description=(
            "`active`, `disabled_by_user` or `disabled_after_failures`. Three values "
            "rather than a boolean, because *SESKit gave up on it* and *you turned it "
            "off* are different facts and an integration should be able to tell them "
            "apart without asking a human."
        ),
        examples=["active"],
    )
    consecutive_failures: int = Field(
        default=0,
        description=(
            "Deliveries that gave up after exhausting their retries, counted since the "
            "last success — one success clears it. Exposed so an application can watch "
            "this climbing before SESKit switches the endpoint off."
        ),
        examples=[0],
    )
    created_at: datetime = Field(description="UTC.")


class WebhookEndpointList(BaseModel):
    data: list[WebhookEndpointResponse] = Field(
        description="Every endpoint on the project, disabled ones included."
    )


class WebhookDeliveryResponse(BaseModel):
    """One attempt to deliver one event to one endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description="Opaque and stable, prefixed `whd_`.",
        examples=["whd_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    event_id: str = Field(
        description="The event being delivered. The same event to two endpoints is two deliveries.",
        examples=["evt_01J8XQ2K3M4N5P6Q7R8S9T0V1W"],
    )
    status: str = Field(
        description="`pending`, `delivered` or `failed`. `failed` means the retries are exhausted.",
        examples=["delivered"],
    )
    attempt_count: int = Field(
        description="Attempts made so far, including the first.", examples=[1]
    )
    response_status: int | None = Field(
        default=None,
        description=(
            "What the endpoint answered. Null when the request never got that far — a "
            "timeout, a refused connection, a destination that failed validation."
        ),
        examples=[200],
    )
    error: str | None = Field(
        default=None,
        description=(
            "The transport failure, normalised. Never a raw exception, which can carry "
            "an address or a URL."
        ),
        examples=[None],
    )
    last_attempt_at: datetime | None = Field(
        default=None, description="Null until the first attempt. UTC."
    )
    next_attempt_at: datetime | None = Field(
        default=None,
        description="When the next retry is due. Null once the delivery has settled. UTC.",
    )
    created_at: datetime = Field(description="UTC.")


class WebhookDeliveryList(BaseModel):
    data: list[WebhookDeliveryResponse] = Field(description="Most recent first.")
