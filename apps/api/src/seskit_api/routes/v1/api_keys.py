"""Read access to a project's API keys (§23).

Listing only. Creating a key requires a key, and an endpoint that mints
credentials without one is a back door - so issuance stays in the dashboard,
behind a session and a CSRF token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from seskit_core.db import get_session
from seskit_core.services import list_api_keys
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, require_api_key
from seskit_api.schemas import APIKeyList, APIKeyResponse
from seskit_api.schemas.errors import ErrorResponse

router = APIRouter(tags=["api-keys"])

#: Documented on every endpoint, so a generated client knows these are possible
#: without having to provoke them.
API_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Invalid or missing API key."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
}


def apply_rate_limit_headers(response: Response, context: APIContext) -> None:
    """Tell the caller what its budget is.

    A client that cannot see its allowance can only discover the limit by
    hitting it, which is a bad way to find out.
    """
    response.headers["X-RateLimit-Limit"] = str(context.rate_limit.limit)
    response.headers["X-RateLimit-Remaining"] = str(context.rate_limit.remaining)
    response.headers["X-RateLimit-Reset"] = str(context.rate_limit.reset_at)


@router.get(
    "/api-keys",
    response_model=APIKeyList,
    responses=API_RESPONSES,
    summary="List API keys",
)
async def list_keys(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> APIKeyList:
    """Every key belonging to the calling key's project.

    Scoped by the authenticated project rather than by anything in the request,
    so there is no parameter a caller could change to see someone else's keys.
    """
    apply_rate_limit_headers(response, context)

    keys = await list_api_keys(db, context.project.id)
    return APIKeyList(data=[APIKeyResponse.model_validate(key) for key in keys])
