"""The scheduled identity re-check.

The job is what makes verification finish without anyone watching: a domain
verifies when DNS propagates, an address when someone clicks a link, and nothing
tells us either has happened.

The job builds its own session and provider - it runs outside a request - so
these tests drive the pieces it composes rather than the ARQ entry point, and
check the registration separately.
"""

from __future__ import annotations

from datetime import timedelta

from fakes.ses import FakeProviderFactory, denied
from seskit_core.models import utcnow
from seskit_core.services import (
    add_identity,
    check_identity,
    create_project,
    identities_due,
    register_user,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
DOMAIN = "example.com"
REGION = "us-east-1"
UNVERIFIED = 6 * 60 * 60
VERIFIED = 30 * 24 * 60 * 60


async def _identity(session: AsyncSession, factory: FakeProviderFactory, value: str = DOMAIN):  # type: ignore[no-untyped-def]
    user = await register_user(
        session, email=f"{value}@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(session, user_id=user.id, name="Sending")
    return await add_identity(session, factory, project_id=project.id, value=value, region=REGION)


# ------------------------------------------------------------ registration ---


def test_the_job_is_registered_as_an_hourly_cron() -> None:
    """A job the worker does not know about would fail silently - the status
    would simply never change, with nothing to look at.
    """
    from seskit_worker.identities import recheck_identities
    from seskit_worker.main import WorkerSettings

    assert recheck_identities in WorkerSettings.functions

    job = WorkerSettings.cron_jobs[0]
    assert job.minute == 0
    assert job.timeout_s == 300


# -------------------------------------------------------------- behaviour ---


async def test_a_due_identity_is_picked_up_and_updated(db_session: AsyncSession) -> None:
    """The whole point: verification completes without anyone pressing
    anything.
    """
    factory = FakeProviderFactory()
    identity = await _identity(db_session, factory)
    identity.last_checked_at = utcnow() - timedelta(days=1)
    await db_session.flush()

    factory.provider.mark_verified(DOMAIN)
    due = await identities_due(db_session, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED)
    for row in due:
        await check_identity(db_session, factory, row)

    assert identity.is_verified is True


async def test_a_recently_checked_identity_is_left_alone(db_session: AsyncSession) -> None:
    """SES throttles, and most identities are settled. Re-asking hourly would
    spend quota to be told the same thing."""
    factory = FakeProviderFactory()
    identity = await _identity(db_session, factory)

    due = await identities_due(db_session, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED)

    assert identity.id not in {row.id for row in due}


async def test_one_failure_does_not_stop_the_pass(db_session: AsyncSession) -> None:
    """A single unreachable domain must not leave every other one stale."""
    factory = FakeProviderFactory()
    broken = await _identity(db_session, factory, value="broken.example")
    healthy = await _identity(db_session, factory, value="healthy.example")
    for row in (broken, healthy):
        row.last_checked_at = utcnow() - timedelta(days=1)
    await db_session.flush()

    factory.provider.mark_verified("healthy.example")
    # The fake fails whatever it is asked next, so check the broken one first
    # and then clear the error, mimicking one bad identity in a longer list.
    factory.provider.error = denied()
    await check_identity(db_session, factory, broken)
    factory.provider.error = None
    await check_identity(db_session, factory, healthy)

    assert broken.last_error is not None
    assert healthy.is_verified is True


async def test_a_failed_check_records_rather_than_raises(db_session: AsyncSession) -> None:
    factory = FakeProviderFactory()
    identity = await _identity(db_session, factory)
    factory.provider.error = denied()

    await check_identity(db_session, factory, identity)

    assert identity.last_error is not None
    assert identity.last_checked_at is not None
