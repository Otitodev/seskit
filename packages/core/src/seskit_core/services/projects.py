"""Project lookup and the ownership boundary.

Every project-scoped read in later phases goes through :func:`get_owned_project`.
Centralising it is the point: an ownership check written inline at each call
site is one someone eventually forgets, and forgetting it here means one
customer reading another's email logs.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import DEFAULT_PROJECT_NAME, Project


async def list_projects(session: AsyncSession, user_id: str) -> list[Project]:
    """Every project belonging to a user, oldest first.

    ULIDs sort by creation time, so ordering by id is chronological and needs no
    extra column.
    """
    result = await session.scalars(
        select(Project).where(Project.user_id == user_id).order_by(Project.id)
    )
    return list(result)


async def get_owned_project(
    session: AsyncSession, *, project_id: str, user_id: str
) -> Project | None:
    """Return the project only if this user owns it.

    Ownership is part of the query rather than a check afterwards, so there is
    no path that loads the row first and forgets to compare.
    """
    project: Project | None = await session.scalar(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    return project


async def get_default_project(session: AsyncSession, user_id: str) -> Project | None:
    """The project to fall back to when none is selected.

    Used when a session carries no project, or names one that has since been
    deleted or was never theirs.
    """
    project: Project | None = await session.scalar(
        select(Project).where(Project.user_id == user_id).order_by(Project.id).limit(1)
    )
    return project


async def create_project(
    session: AsyncSession, *, user_id: str, name: str = DEFAULT_PROJECT_NAME
) -> Project:
    project = Project(user_id=user_id, name=name)
    session.add(project)
    await session.flush()
    return project
