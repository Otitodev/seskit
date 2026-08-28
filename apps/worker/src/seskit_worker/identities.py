"""Keeping identity verification current (§10).

Verification is asynchronous - a domain can take minutes or days, and an email
address waits on a human clicking a link. Nothing tells us when it completes, so
something has to ask.

Asking is deliberately unevenly spaced. An identity still waiting is re-checked
every few hours; one already verified, once a month. The rare case is the one
worth having: it is what catches a DKIM record deleted long after setup, which
otherwise looks healthy right up until a send fails.

One failure must not stop the pass. Each identity is checked in its own
try/except and a failure is recorded on the row, because a single unreachable
domain should not leave every other one stale.
"""

from __future__ import annotations

from typing import Any

from seskit_core.config import get_settings
from seskit_core.db import get_session_factory
from seskit_core.logging import get_logger
from seskit_core.services import check_identity, identities_due
from seskit_provider_aws_ses import SESProvider

logger = get_logger(__name__)


async def recheck_identities(ctx: dict[str, Any]) -> int:
    """Re-ask SES about every identity that is due. Returns how many were checked.

    Runs hourly, but the hourly tick is not the interval - the per-identity due
    check is. Most passes will find nothing to do, which is the intended shape:
    the schedule is cheap and the API calls are not.
    """
    settings = get_settings()
    factory = get_session_factory()
    checked = 0

    async with factory() as session:
        due = await identities_due(
            session,
            unverified_seconds=settings.IDENTITY_RECHECK_UNVERIFIED_SECONDS,
            verified_seconds=settings.IDENTITY_RECHECK_VERIFIED_SECONDS,
        )

        for identity in due:
            try:
                await check_identity(session, SESProvider, identity)
                checked += 1
            except Exception:
                # check_identity already records an APIError on the row; this
                # catches anything it could not, so one broken identity cannot
                # abandon the rest of the pass.
                logger.exception(
                    "identity_recheck_failed",
                    identity_id=identity.id,
                    job_id=ctx.get("job_id"),
                )

        await session.commit()

    if checked:
        logger.info("identity_recheck_pass", checked=checked, due=len(due))
    return checked
