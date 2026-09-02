"""``GET /v1/webhooks`` and its delivery history (§23).

Read-only, and that is the whole shape of it. Registering a destination means
handing SESKit a URL and asking it to make requests, which is checked against
the SSRF policy and is a deliberate act by a human who is signed in - so
creation, rotation and deletion stay on the dashboard behind a session and a
CSRF token, the same reasoning that keeps API key *issuance* off the public API.

What an application legitimately wants here is to see its own integration: which
endpoints exist, whether SESKit is still sending to them, and what happened to
recent deliveries. That is what this returns.

Scoped by the key's project, so there is no request parameter to tamper with.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from seskit_core.db import get_session
from seskit_core.errors import APIError, ErrorType
from seskit_core.services import get_owned_endpoint, list_deliveries, list_endpoints
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, require_api_key
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.webhooks import (
    WebhookDeliveryList,
    WebhookDeliveryResponse,
    WebhookEndpointList,
    WebhookEndpointResponse,
)

router = APIRouter(tags=["webhooks"])

#: Deliveries returned per request. Enough to see what is happening now; a
#: complete history belongs to a paginated endpoint, which §23 does not ask for
#: and which would be the wrong shape to guess at.
DELIVERY_LIMIT = 50


@router.get(
    "/webhooks",
    response_model=WebhookEndpointList,
    responses=API_RESPONSES,
    summary="List webhook endpoints",
)
async def list_webhook_endpoints(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> WebhookEndpointList:
    """Every webhook endpoint registered on this key's project.

    The signing secret is not included - see the note in
    ``schemas/webhooks.py``. It is available on the dashboard, where it is read
    once by a person rather than returned to code on every call.
    """
    apply_rate_limit_headers(response, context)

    endpoints = await list_endpoints(db, context.project.id)
    return WebhookEndpointList(
        data=[WebhookEndpointResponse.model_validate(endpoint) for endpoint in endpoints]
    )


@router.get(
    "/webhooks/{endpoint_id}/deliveries",
    response_model=WebhookDeliveryList,
    responses=API_RESPONSES,
    summary="List recent webhook deliveries",
)
async def list_webhook_deliveries(
    endpoint_id: str,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> WebhookDeliveryList:
    """Recent delivery attempts for one endpoint, newest first.

    The delivery row is the queue as well as the log, so what comes back is the
    actual state of each attempt - including one still pending a retry, with the
    time it is next due.
    """
    apply_rate_limit_headers(response, context)

    endpoint = await get_owned_endpoint(db, project_id=context.project.id, endpoint_id=endpoint_id)
    if endpoint is None:
        # The same answer whether it belongs to another project or does not
        # exist, so a caller cannot probe for real ids.
        raise APIError(ErrorType.NOT_FOUND, "No such webhook endpoint.")

    deliveries = await list_deliveries(db, endpoint_id=endpoint.id, limit=DELIVERY_LIMIT)
    return WebhookDeliveryList(
        data=[WebhookDeliveryResponse.model_validate(delivery) for delivery in deliveries]
    )
