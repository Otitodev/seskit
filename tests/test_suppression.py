"""The suppression list (§31 Phase 11).

The list is only worth having if it is impossible to route around and possible
to undo. These are the properties that make it either: an address stored one
way and looked up another is a list that silently does nothing, and a removal
that cannot be followed by a second suppression is a table that stops working
some months after it looked correct.
"""

from __future__ import annotations

import pytest
from seskit_core.models import SuppressedAddress, SuppressionReason
from seskit_core.services import (
    create_project,
    find_suppression,
    list_suppressions,
    register_user,
    remove_suppression,
    suppress,
    suppressed_among,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
ADDRESS = "bounced@example.com"


async def _project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return project.id


# --------------------------------------------------------------- adding ---


async def test_an_address_can_be_suppressed(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)

    row = await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )

    assert row.address == ADDRESS
    assert row.reason == "bounce"
    assert row.is_active is True
    assert row.id.startswith("supp_")


async def test_suppressing_twice_does_not_raise_or_duplicate(db_session: AsyncSession) -> None:
    """SES can redeliver a notification, and an address can bounce again next
    week. Neither is an error, and neither should leave two live rows.
    """
    project_id = await _project(db_session)

    first = await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    second = await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.COMPLAINT
    )

    assert second.id == first.id
    assert second.reason == "bounce", "the first reason is the true one"

    rows = await db_session.scalars(select(SuppressedAddress))
    assert len(list(rows)) == 1


# -------------------------------------------------------- normalisation ---


@pytest.mark.parametrize(
    "written",
    [
        "Bob <bounced@example.com>",
        "BOUNCED@EXAMPLE.COM",
        "  bounced@example.com  ",
        '"Bob Smith" <Bounced@Example.com>',
    ],
)
async def test_a_suppressed_address_cannot_be_written_around(
    db_session: AsyncSession, written: str
) -> None:
    """The property the whole list depends on.

    If adding a display name or capitalising the domain got a message through,
    the list would not be suppression - it would be a suggestion.
    """
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )

    found = await find_suppression(db_session, project_id=project_id, address=written)

    assert found is not None


async def test_it_is_stored_normalised_not_as_written(db_session: AsyncSession) -> None:
    """Normalised on the way in as well as on the way out, so the unique index
    is doing the work rather than agreeing by luck.
    """
    project_id = await _project(db_session)

    row = await suppress(
        db_session,
        project_id=project_id,
        address="Bob <Bounced@Example.COM>",
        reason=SuppressionReason.BOUNCE,
    )

    assert row.address == "bounced@example.com"


# ------------------------------------------------------------- scoping ---


async def test_suppression_does_not_leak_between_projects(db_session: AsyncSession) -> None:
    """The reason SESKit owns this list rather than SES.

    SES's own list belongs to the AWS account, and every project on an instance
    resolves the same credentials - so this separation is exactly what the
    account-level list cannot express, and is the whole argument for the table.
    """
    staging = await _project(db_session, email="owner@example.com")
    production = await _project(db_session, email="other@example.com")

    await suppress(
        db_session, project_id=staging, address=ADDRESS, reason=SuppressionReason.COMPLAINT
    )

    assert await find_suppression(db_session, project_id=staging, address=ADDRESS) is not None
    assert await find_suppression(db_session, project_id=production, address=ADDRESS) is None


# -------------------------------------------------------- the send path ---


async def test_the_send_path_asks_about_every_recipient_at_once(
    db_session: AsyncSession,
) -> None:
    """One query for the whole list. Per-recipient lookups would put a round
    trip per address on every message.
    """
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await suppress(
        db_session,
        project_id=project_id,
        address="also@example.com",
        reason=SuppressionReason.MANUAL,
    )

    blocked = await suppressed_among(
        db_session,
        project_id=project_id,
        addresses=["fine@example.com", "Bob <BOUNCED@example.com>", "also@example.com"],
    )

    assert blocked == {"bounced@example.com", "also@example.com"}


async def test_nothing_suppressed_means_no_query_result_not_an_error(
    db_session: AsyncSession,
) -> None:
    project_id = await _project(db_session)

    assert await suppressed_among(db_session, project_id=project_id, addresses=["a@b.com"]) == set()
    assert await suppressed_among(db_session, project_id=project_id, addresses=[]) == set()


# ------------------------------------------------------------- removing ---


async def test_removing_takes_an_address_off_the_list(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )

    changed = await remove_suppression(db_session, project_id=project_id, address=ADDRESS)

    assert changed is True
    assert await find_suppression(db_session, project_id=project_id, address=ADDRESS) is None


async def test_removing_keeps_the_row(db_session: AsyncSession) -> None:
    """A suppressed address is a support question. "Was this ever suppressed,
    and who took it off" has to survive the removal.
    """
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )

    await remove_suppression(db_session, project_id=project_id, address=ADDRESS)

    row = await db_session.scalar(select(SuppressedAddress))
    assert row is not None
    assert row.removed_at is not None
    assert row.reason == "bounce", "why it was suppressed outlives the removal"


async def test_removing_something_absent_changes_nothing(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)

    assert await remove_suppression(db_session, project_id=project_id, address=ADDRESS) is False


async def test_an_address_can_be_suppressed_again_after_removal(db_session: AsyncSession) -> None:
    """What the partial unique index exists for.

    A plain unique constraint passes every other test in this file and fails
    here - months later, in production, when a cleared address bounces again.
    """
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await remove_suppression(db_session, project_id=project_id, address=ADDRESS)

    again = await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.COMPLAINT
    )

    assert again.is_active is True
    assert again.reason == "complaint"

    rows = list(await db_session.scalars(select(SuppressedAddress)))
    assert len(rows) == 2, "the history is two rows, not an overwrite"


# ------------------------------------------------------------- listing ---


async def test_the_list_shows_only_live_suppressions(db_session: AsyncSession) -> None:
    """The dashboard's question is "who am I not sending to". Answering it with
    rows that no longer apply would make the page read worse than the truth.
    """
    project_id = await _project(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await suppress(
        db_session,
        project_id=project_id,
        address="gone@example.com",
        reason=SuppressionReason.MANUAL,
    )
    await remove_suppression(db_session, project_id=project_id, address="gone@example.com")

    live = await list_suppressions(db_session, project_id=project_id)
    everything = await list_suppressions(db_session, project_id=project_id, include_removed=True)

    assert [row.address for row in live] == [ADDRESS]
    assert len(everything) == 2


async def test_a_note_survives_for_a_manual_entry(db_session: AsyncSession) -> None:
    """Support added it by hand; the next person needs to know why."""
    project_id = await _project(db_session)

    row = await suppress(
        db_session,
        project_id=project_id,
        address=ADDRESS,
        reason=SuppressionReason.MANUAL,
        note="Asked us to stop, ticket #4412",
    )

    assert row.note == "Asked us to stop, ticket #4412"
