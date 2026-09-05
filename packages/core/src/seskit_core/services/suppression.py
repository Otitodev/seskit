"""Reading and writing the suppression list.

Every address that enters or leaves the list goes through here, so the
normalisation is applied in one place. An address stored one way and looked up
another is a list that silently does nothing, and that failure is invisible
until somebody's bounce rate has already climbed.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seskit_core.email import bare_address
from seskit_core.logging import get_logger
from seskit_core.models import SuppressedAddress, SuppressionReason
from seskit_core.models.base import utcnow

logger = get_logger(__name__)


async def suppress(
    session: AsyncSession,
    *,
    project_id: str,
    address: str,
    reason: SuppressionReason,
    source_event_id: str | None = None,
    note: str | None = None,
) -> SuppressedAddress:
    """Add an address to a project's list, or return the row already there.

    Idempotent on purpose. The same address bounces on Monday and again on
    Thursday, and SES can redeliver a notification at any time; neither should
    be an error, and neither should overwrite the reason the address was first
    suppressed. The first answer is the true one - a complaint that was later
    followed by a bounce is still a complaint.
    """
    value = bare_address(address)

    existing = await find_suppression(session, project_id=project_id, address=value)
    if existing is not None:
        return existing

    row = SuppressedAddress(
        project_id=project_id,
        address=value,
        reason=reason.value,
        source_event_id=source_event_id,
        note=note,
    )
    session.add(row)
    await session.flush()

    logger.info(
        "address_suppressed",
        project_id=project_id,
        reason=reason.value,
        source_event_id=source_event_id,
    )
    return row


async def find_suppression(
    session: AsyncSession, *, project_id: str, address: str
) -> SuppressedAddress | None:
    """The live suppression for this address, if there is one.

    Removed rows are deliberately not returned. They are history, and treating
    a removed row as a match would make a removal impossible to act on.
    """
    row: SuppressedAddress | None = await session.scalar(
        select(SuppressedAddress).where(
            SuppressedAddress.project_id == project_id,
            SuppressedAddress.address == bare_address(address),
            SuppressedAddress.removed_at.is_(None),
        )
    )
    return row


async def suppressed_among(
    session: AsyncSession, *, project_id: str, addresses: Iterable[str]
) -> set[str]:
    """Which of these addresses are suppressed, as normalised addresses.

    One query for the whole recipient list rather than one per recipient: this
    runs on the send path, where it is pure added latency on every message that
    is fine.

    Returns the normalised forms, not the caller's spellings, so a caller that
    wants to name the offending address in an error reduces theirs the same way
    rather than guessing which of them matched.
    """
    wanted = {bare_address(value) for value in addresses}
    wanted.discard("")
    if not wanted:
        return set()

    rows = await session.scalars(
        select(SuppressedAddress.address).where(
            SuppressedAddress.project_id == project_id,
            SuppressedAddress.address.in_(wanted),
            SuppressedAddress.removed_at.is_(None),
        )
    )
    return set(rows)


async def remove_suppression(session: AsyncSession, *, project_id: str, address: str) -> bool:
    """Take an address off the list. Returns whether anything changed.

    Soft: the row stays and gets a ``removed_at``. Suppressing the same address
    again later writes a new row, which is why the unique index is partial -
    "bounced in March, cleared in April, complained in June" is a history
    somebody will need, and a DELETE throws it away.
    """
    row = await find_suppression(session, project_id=project_id, address=address)
    if row is None:
        return False

    row.removed_at = utcnow()
    await session.flush()

    logger.info("suppression_removed", project_id=project_id, suppression_id=row.id)
    return True


async def list_suppressions(
    session: AsyncSession, *, project_id: str, include_removed: bool = False
) -> list[SuppressedAddress]:
    """A project's list, newest first.

    ``include_removed`` is off by default because the dashboard's question is
    "who am I not sending to", and answering it with rows that no longer apply
    would make the page read as worse than the truth.
    """
    query = (
        select(SuppressedAddress)
        # Eager, because the dashboard shows which message caused each entry and
        # a lazy load on an async session raises rather than quietly querying.
        .options(selectinload(SuppressedAddress.source_event))
        .where(SuppressedAddress.project_id == project_id)
    )
    if not include_removed:
        query = query.where(SuppressedAddress.removed_at.is_(None))

    rows = await session.scalars(query.order_by(SuppressedAddress.id.desc()))
    return list(rows)
