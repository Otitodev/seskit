"""The page a recipient reaches from the Unsubscribe button (§31 Phase 11).

The only public, unauthenticated, HTML-serving routes in SESKit. Everything
else here belongs to somebody with an account; this belongs to a person who
received an email and would like it to stop, and who may not know SESKit
exists.

**GET never changes anything.** Mail clients and security scanners follow links
in messages without being asked, so a GET that unsubscribed would unsubscribe
people who did nothing. The GET shows a button; the POST does the work. That is
also what RFC 8058 specifies - the one-click POST is what Gmail and Outlook
send, and it carries ``List-Unsubscribe=One-Click`` in the body rather than any
session or token of their own.

**There is no CSRF token on the POST, deliberately.** The signed token in the
URL *is* the authorisation, and it has to be, because the sender of the POST is
a mail provider with no session here. Forging one means forging an HMAC.

**Every answer is 200.** A bad token, an unknown message and a successful
unsubscribe are all a page and a 200, so nobody can use these routes to learn
whether an address, a message or a project exists. The body tells the person
holding a real token what happened; it tells someone guessing nothing they did
not already supply.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.events import record_suppression_event
from seskit_core.logging import get_logger
from seskit_core.models import Email, SuppressionReason
from seskit_core.security.unsubscribe import read_token, token_matches
from seskit_core.services import find_suppression, suppress
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import get_app_settings
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["unsubscribe"], include_in_schema=False)


async def _resolve(db: AsyncSession, settings: Settings, token: str) -> tuple[Email, str] | None:
    """The message and address a token names, if this instance issued it.

    ``None`` covers "unreadable", "signature does not verify" and "no such
    message" without distinguishing them, and the page says the same sentence
    for all three. Telling them apart is exactly the distinction an oracle
    would offer.

    Reading comes before verifying because the project id that keys the
    signature is only known once the message has been found, and the message can
    only be found once the token has been read. The lookup in between is a
    primary-key select on a value the caller supplied - bounded, indexed, and
    unable to return anything belonging to another project by accident, because
    the signature is checked against whatever project the row turns out to be
    in.
    """
    parsed = read_token(token)
    if parsed is None:
        return None

    email_id, address = parsed
    email = await db.scalar(select(Email).where(Email.id == email_id))
    if email is None:
        return None

    if not token_matches(settings.SECRET_KEY, project_id=email.project_id, token=token):
        return None
    return email, address


def _page(
    request: Request,
    *,
    address: str | None = None,
    token: str = "",
    done: bool = False,
    invalid: bool = False,
) -> HTMLResponse:
    return render(
        request,
        "pages/unsubscribe.html",
        address=address,
        token=token,
        done=done,
        invalid=invalid,
    )


@router.get("/u/{token}", response_class=HTMLResponse, summary="Confirm an unsubscribe")
async def confirm(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HTMLResponse:
    """Ask before doing anything, and say if it has already been done."""
    resolved = await _resolve(db, settings, token)
    if resolved is None:
        return _page(request, invalid=True)

    email, address = resolved
    existing = await find_suppression(db, project_id=email.project_id, address=address)
    return _page(request, address=address, token=token, done=existing is not None)


@router.post("/u/{token}", response_class=HTMLResponse, summary="Unsubscribe")
async def unsubscribe(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HTMLResponse:
    """Stop sending to this address, for this project.

    Idempotent: pressing the button twice, or a mail provider retrying its
    one-click POST, produces the same page and no second event. ``suppress``
    already returns the existing row rather than writing another, so the only
    thing to decide here is whether anything actually changed.
    """
    resolved = await _resolve(db, settings, token)
    if resolved is None:
        return _page(request, invalid=True)

    email, address = resolved
    already = await find_suppression(db, project_id=email.project_id, address=address)
    if already is None:
        await suppress(
            db,
            project_id=email.project_id,
            address=address,
            reason=SuppressionReason.UNSUBSCRIBE,
            note="Unsubscribed from an email.",
        )
        # Reported like a bounce-driven suppression, so an application keeping
        # its own mailing list in step hears about every way an address can
        # leave. No causing event: the recipient told SESKit directly.
        await record_suppression_event(
            db,
            email_id=email.id,
            addresses=[address],
            reason=SuppressionReason.UNSUBSCRIBE,
        )
        await db.commit()
        logger.info("unsubscribed", email_id=email.id, project_id=email.project_id)

    return _page(request, address=address, token=token, done=True)
