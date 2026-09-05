"""The Suppressions dashboard page (§31 Phase 11).

The list worked before this page existed - bounces filled it and sends were
refused against it. What it could not do was answer *"why can I not email this
person?"*, which arrives as a support ticket rather than a stack trace.

So what is worth testing is what the page lets someone do about a suppression,
and what it stops them doing: removal has to work, it has to be scoped to the
project they are looking at, and it must not be reachable by a GET.
"""

from __future__ import annotations

from httpx import AsyncClient
from seskit_core.models import Project, SuppressionReason
from seskit_core.services import find_suppression, suppress
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ADDRESS = "bounced@example.com"


async def _csrf(client: AsyncClient, path: str = "/suppressions") -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _project_id(session: AsyncSession) -> str:
    project = await session.scalar(select(Project))
    assert project is not None
    return project.id


# ---------------------------------------------------------------- reading ---


async def test_the_empty_page_says_that_is_the_healthy_state(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """An empty list is not a setup step. "Get started" here would invite
    someone to create a problem they do not have.
    """
    page = await signed_in_client.get("/suppressions")

    assert page.status_code == 200
    assert "Nothing is suppressed" in page.text
    assert "automatically" in page.text


async def test_a_suppressed_address_is_listed_with_why_and_when(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id = await _project_id(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await db_session.commit()

    page = await signed_in_client.get("/suppressions")

    assert ADDRESS in page.text
    assert "Hard bounce" in page.text


async def test_a_complaint_is_not_described_as_a_bounce(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """They are different facts and lead to different decisions about letting
    someone back on the list.
    """
    project_id = await _project_id(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.COMPLAINT
    )
    await db_session.commit()

    page = await signed_in_client.get("/suppressions")

    assert "Marked as spam" in page.text
    assert "Hard bounce" not in page.text


async def test_a_note_is_shown(signed_in_client: AsyncClient, db_session: AsyncSession) -> None:
    """Support wrote down why. The next person reading this needs it."""
    project_id = await _project_id(db_session)
    await suppress(
        db_session,
        project_id=project_id,
        address=ADDRESS,
        reason=SuppressionReason.MANUAL,
        note="Asked us to stop, ticket #4412",
    )
    await db_session.commit()

    page = await signed_in_client.get("/suppressions")

    assert "ticket #4412" in page.text


# ---------------------------------------------------------------- adding ---


async def test_an_address_can_be_suppressed_by_hand(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The support case automation cannot cover: someone asks to be left alone
    and there is no bounce to record it.
    """
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions",
        data={"csrf_token": token, "address": ADDRESS, "note": "Asked by email"},
    )

    assert response.status_code == 200
    project_id = await _project_id(db_session)
    assert await find_suppression(db_session, project_id=project_id, address=ADDRESS) is not None


async def test_adding_says_it_happened(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions", data={"csrf_token": token, "address": ADDRESS, "note": ""}
    )

    assert "will not be emailed" in response.text


async def test_something_that_is_not_an_address_is_refused(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions", data={"csrf_token": token, "address": "not-an-address", "note": ""}
    )

    assert response.status_code == 400
    assert "is not an email address" in response.text


# -------------------------------------------------------------- removing ---


async def test_an_address_can_be_allowed_again(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The whole reason the page exists."""
    project_id = await _project_id(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await db_session.commit()
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions/remove", data={"csrf_token": token, "address": ADDRESS}
    )

    assert response.status_code == 200
    assert await find_suppression(db_session, project_id=project_id, address=ADDRESS) is None


async def test_removing_says_mail_will_be_delivered_again(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The consequence, not the bookkeeping. Somebody is about to send to an
    address that bounced once already.
    """
    project_id = await _project_id(db_session)
    await suppress(
        db_session, project_id=project_id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await db_session.commit()
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions/remove", data={"csrf_token": token, "address": ADDRESS}
    )

    assert "can be emailed again" in response.text


async def test_removing_something_absent_is_not_an_error(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A second click on a stale tab should show the truth, not a failure."""
    token = await _csrf(signed_in_client)

    response = await signed_in_client.post(
        "/suppressions/remove", data={"csrf_token": token, "address": ADDRESS}
    )

    assert response.status_code == 200


# --------------------------------------------------------------- refusing ---


async def test_neither_action_is_reachable_by_a_get(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both change state. A link would let any page on the internet trigger one
    with an image tag, and un-suppressing someone that way is a good example of
    why that matters.
    """
    added = await signed_in_client.get("/suppressions?address=x@example.com")
    removed = await signed_in_client.get("/suppressions/remove?address=x@example.com")

    assert added.status_code == 200, "the page itself still renders"
    assert removed.status_code == 405


async def test_an_action_without_a_csrf_token_is_refused(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await signed_in_client.post("/suppressions", data={"address": ADDRESS, "note": ""})

    assert response.status_code == 403


async def test_a_suppression_belonging_to_another_project_is_untouched(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Removal is scoped to the project on screen, so a posted address cannot
    reach a list the person is not looking at.
    """
    from seskit_core.services import create_project

    owner = await db_session.scalar(select(Project))
    assert owner is not None
    mine = owner.id
    other = await create_project(db_session, user_id=owner.user_id, name="Other")
    await suppress(
        db_session, project_id=other.id, address=ADDRESS, reason=SuppressionReason.BOUNCE
    )
    await db_session.commit()
    token = await _csrf(signed_in_client)

    await signed_in_client.post(
        "/suppressions/remove", data={"csrf_token": token, "address": ADDRESS}
    )

    assert mine != other.id
    assert await find_suppression(db_session, project_id=other.id, address=ADDRESS) is not None
