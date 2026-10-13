"""``GET /v1/domains`` (§23).

Domain identities only. Email-address identities exist to get a human sending
in minutes without touching DNS; an application managing sending infrastructure
wants domains, and §23 names this endpoint accordingly. Exposing addresses here
later is additive.

Scoped by the key's project, so there is no request parameter to tamper with.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from seskit_core.db import get_session
from seskit_core.models import Identity
from seskit_core.providers import IdentityType
from seskit_core.services import list_identities
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, require_api_key
from seskit_api.receiving import mx_record
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.domains import (
    DomainList,
    DomainResponse,
    MxRecordResponse,
    ReceivingResponse,
)

router = APIRouter(tags=["domains"])


@router.get(
    "/domains",
    response_model=DomainList,
    responses=API_RESPONSES,
    summary="List sending domains",
)
async def list_domains(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> DomainList:
    """Every domain this key's project can send from."""
    apply_rate_limit_headers(response, context)

    identities = await list_identities(db, context.project.id)
    domains = [identity for identity in identities if identity.type is IdentityType.DOMAIN]

    return DomainList(data=[_response(domain) for domain in domains])


def _response(identity: Identity) -> DomainResponse:
    """A domain as a customer sees it, with whether it receives mail.

    ``receiving`` is attached here and not read off the row, because the MX host is
    Amazon SES's and the row is core's, which knows nothing about a provider. The
    record is withheld until receiving is on, so nobody publishes it early.
    """
    response = DomainResponse.model_validate(identity)
    record = mx_record(identity)
    response.receiving = ReceivingResponse(
        enabled=identity.receives_mail,
        mx=(
            MxRecordResponse(
                record_type="MX", name=record.name, priority=record.priority, value=record.value
            )
            if identity.receives_mail
            else None
        ),
    )
    return response
