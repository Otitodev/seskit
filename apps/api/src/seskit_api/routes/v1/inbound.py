"""``GET /v1/inbound`` and ``GET /v1/inbound/{id}`` - mail the project received.

The read side of receiving. Nothing here writes: a message is recorded by the
worker as it arrives, and this only says what is there.

Everything under ``/v1`` is scoped to the key's project in the query itself, so
an id belonging to another project resolves to a 404 and not a 403 - which
would confirm it exists.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response
from seskit_core.db import get_session
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import InboundEmail
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, require_api_key
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.inbound import (
    InboundList,
    InboundResponse,
    detail_of,
    summary_of,
)

router = APIRouter(tags=["inbound"])

#: How many messages one page returns when the caller does not say.
DEFAULT_PAGE = 25

#: The most one page will ever return. A cap and not a suggestion, as on
#: ``/v1/emails``: without one a project with a year of mail can ask for all of
#: it in a single query.
MAX_PAGE = 100


async def _owned(db: AsyncSession, inbound_id: str, project_id: str) -> InboundEmail | None:
    """One received message, if it belongs to this project.

    Ownership is part of the query, so an id from another project is
    indistinguishable from one that never existed.
    """
    found: InboundEmail | None = await db.scalar(
        select(InboundEmail).where(
            InboundEmail.id == inbound_id, InboundEmail.project_id == project_id
        )
    )
    return found


@router.get(
    "/inbound/{inbound_id}",
    response_model=InboundResponse,
    responses=API_RESPONSES,
    summary="Retrieve a received email",
)
async def get_inbound(
    response: Response,
    inbound_id: Annotated[
        str, Path(description="The id from the list, or from an `email.received` event.")
    ],
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> InboundResponse:
    """One message, parsed. Bodies, headers, attachments and what SES concluded.

    The bodies are the sender's, not SESKit's. In particular the HTML is whatever
    was sent, and has to be treated as hostile by anything that renders it.
    """
    apply_rate_limit_headers(response, context)

    row = await _owned(db, inbound_id, context.project.id)
    if row is None:
        raise APIError(ErrorType.NOT_FOUND, "No received email with that id.")
    return detail_of(row)


@router.get(
    "/inbound",
    response_model=InboundList,
    responses=API_RESPONSES,
    summary="List received emails",
)
async def list_inbound(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE, description="How many messages to return, newest first."),
    ] = DEFAULT_PAGE,
    starting_after: Annotated[
        str | None,
        Query(
            description=(
                "Return messages older than this id - the last id from the previous "
                "page. The id must belong to this project."
            ),
        ),
    ] = None,
    domain: Annotated[
        str | None,
        Query(description="Only messages received for this domain."),
    ] = None,
) -> InboundList:
    """This key's project, newest first, without bodies.

    Paged by cursor for the reason ``/v1/emails`` is: ids sort in the order they
    were created, so ``starting_after`` names a fixed point and stays correct
    while mail arrives underneath it, where an offset would silently skip the
    message that moved across the boundary. An unknown ``starting_after`` is a 404
    and not an empty page, because the comparison is lexical and an id from
    another project would otherwise position a page of real messages by a value
    the caller cannot see.

    Sorted by id and not by ``received_at``. A backlog means a message can be
    recorded long after it arrived, so the two orders can differ; id order is the
    one a cursor can be stable over, and ``received_at`` is on every row.
    """
    apply_rate_limit_headers(response, context)

    query = select(InboundEmail).where(InboundEmail.project_id == context.project.id)

    if starting_after is not None:
        cursor = await db.scalar(
            select(InboundEmail.id).where(
                InboundEmail.id == starting_after,
                InboundEmail.project_id == context.project.id,
            )
        )
        if cursor is None:
            raise APIError(ErrorType.NOT_FOUND, "No received email with that id to page from.")
        query = query.where(InboundEmail.id < cursor)

    if domain is not None:
        query = query.where(InboundEmail.domain == domain.strip().lower())

    # One more than asked for, so "is there another page?" is answered by the
    # query that fetched this one.
    rows = list(await db.scalars(query.order_by(InboundEmail.id.desc()).limit(limit + 1)))

    return InboundList(
        data=[summary_of(row) for row in rows[:limit]],
        has_more=len(rows) > limit,
    )
