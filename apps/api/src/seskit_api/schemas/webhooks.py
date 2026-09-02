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

    id: str = Field(examples=["wh_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    url: str = Field(examples=["https://example.com/webhooks/email"])
    #: ``active`` | ``disabled_by_user`` | ``disabled_after_failures``. Three
    #: values rather than a boolean, because "SESKit gave up on it" and "you
    #: turned it off" are different facts and an integration should be able to
    #: tell them apart without asking a human.
    status: str = Field(examples=["active"])
    consecutive_failures: int = Field(default=0, examples=[0])
    created_at: datetime


class WebhookEndpointList(BaseModel):
    data: list[WebhookEndpointResponse]


class WebhookDeliveryResponse(BaseModel):
    """One attempt to deliver one event to one endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(examples=["whd_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    event_id: str = Field(examples=["evt_01J8XQ2K3M4N5P6Q7R8S9T0V1W"])
    #: ``pending`` | ``delivered`` | ``failed``.
    status: str = Field(examples=["delivered"])
    attempt_count: int = Field(examples=[1])
    #: What the endpoint answered. Null when the request never got that far -
    #: a timeout, a refused connection, a destination that failed validation.
    response_status: int | None = Field(default=None, examples=[200])
    #: The transport failure, normalised. Never a raw exception, which can
    #: carry an address or a URL.
    error: str | None = Field(default=None, examples=[None])
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime


class WebhookDeliveryList(BaseModel):
    data: list[WebhookDeliveryResponse]
