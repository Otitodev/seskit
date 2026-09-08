"""How far through setting up a project somebody is.

A new instance is four steps from sending real mail, and until now none of them
were named anywhere except the documentation. Somebody who installed SESKit and
opened the dashboard saw an empty Overview and had to work out what to do from
a nav bar.

**The order is the friction ladder, not the dependency graph.** Sending works
before AWS exists - a message with no connection goes to Mailpit - so trying it
comes first and connecting an account second. Ordering by what depends on what
would put the slowest, most external step in front of the fastest one, and lose
the thing that makes SESKit worth trying at all.

Read rather than recorded. There is no `onboarding_completed` column, because a
column can disagree with reality: a project whose only API key was revoked has
not still "created an API key" in any sense that helps. Four cheap queries
against state that is already the truth cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import APIKey, AWSConnection, ConnectionStatus, Email, Identity
from seskit_core.providers.types import VerificationStatus


@dataclass(frozen=True, slots=True)
class SetupStep:
    """One thing to do, and whether it is done.

    ``why`` is the reason to bother rather than a restatement of the title. A
    checklist that says "Connect AWS: connect your AWS account" has told the
    reader nothing they did not get from the heading.
    """

    title: str
    why: str
    href: str
    done: bool


async def setup_progress(session: AsyncSession, project_id: str) -> list[SetupStep]:
    """The four steps, in the order worth doing them."""
    keys = await session.scalar(
        select(func.count())
        .select_from(APIKey)
        .where(APIKey.project_id == project_id, APIKey.revoked_at.is_(None))
    )
    sent = await session.scalar(
        select(func.count()).select_from(Email).where(Email.project_id == project_id)
    )
    connected = await session.scalar(
        select(func.count())
        .select_from(AWSConnection)
        .where(
            AWSConnection.project_id == project_id,
            AWSConnection.status == ConnectionStatus.CONNECTED.value,
        )
    )
    verified = await session.scalar(
        select(func.count())
        .select_from(Identity)
        .where(
            Identity.project_id == project_id,
            Identity.verification_status == VerificationStatus.SUCCESS.value,
        )
    )

    return [
        SetupStep(
            title="Create an API key",
            why="What your application sends with. Shown once, so keep it.",
            href="/api-keys",
            done=bool(keys),
        ),
        SetupStep(
            title="Send a test message",
            why="Works before AWS exists - it goes to Mailpit, not the internet.",
            href="/emails",
            done=bool(sent),
        ),
        SetupStep(
            title="Connect an AWS account",
            why="Paste an access key. Until you do, nothing leaves this machine.",
            href="/aws",
            done=bool(connected),
        ),
        SetupStep(
            title="Verify a sender",
            why="Amazon will only send from an address or domain you have proved you own.",
            href="/domains",
            done=bool(verified),
        ),
    ]


def is_complete(steps: list[SetupStep]) -> bool:
    """Whether to stop showing the checklist.

    Every step, not the first few. A project that can send through SES but has
    verified no sender is the case where the last step is the one that matters,
    and hiding the list at three of four would hide exactly that.
    """
    return all(step.done for step in steps)
