"""The setup checklist (§31, product work deferred from Phase 10).

A new instance is four steps from sending real mail, and none of them were
named anywhere except the documentation. Somebody who installed SESKit and
opened the dashboard saw an empty Overview and a nav bar, and had to work out
the order for themselves.

What is worth testing is the order and the honesty. The order is the friction
ladder rather than the dependency graph — sending works before AWS exists — and
the honesty is that every step is read from state rather than recorded, so a
project cannot be told it has done something it has since undone.
"""

from __future__ import annotations

from fakes.ses import ACCOUNT_ID, FAKE_CREDENTIALS, TEST_SECRET_KEY
from httpx import AsyncClient
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailStatus,
    Identity,
    utcnow,
)
from seskit_core.providers.types import IdentityType, VerificationStatus
from seskit_core.security.aws_credentials import encrypt_secret_access_key
from seskit_core.services import (
    SetupStep,
    create_api_key,
    create_project,
    is_complete,
    register_user,
    setup_progress,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"


async def _project(session: AsyncSession, *, owner: str = "owner@example.com") -> str:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return str(project.id)


def _titles(steps: list[SetupStep]) -> list[str]:
    return [step.title for step in steps]


def _done(steps: list[SetupStep]) -> list[str]:
    return [step.title for step in steps if step.done]


# ----------------------------------------------------------------- order ---


async def test_a_new_project_has_everything_left_to_do(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)

    steps = await setup_progress(db_session, project_id)

    assert _done(steps) == []
    assert is_complete(steps) is False


async def test_trying_it_comes_before_connecting_aws(db_session: AsyncSession) -> None:
    """The order is the friction ladder, not the dependency graph.

    A send works before AWS exists - it goes to Mailpit. Ordering by what
    depends on what would put the slowest, most external step in front of the
    fastest one, and lose the thing that makes SESKit worth trying at all.
    """
    project_id = await _project(db_session)

    titles = _titles(await setup_progress(db_session, project_id))

    assert titles.index("Send a test message") < titles.index("Connect an AWS account")
    assert titles.index("Create an API key") < titles.index("Send a test message")
    assert titles.index("Connect an AWS account") < titles.index("Verify a sender")


async def test_every_step_says_why_rather_than_repeating_itself(
    db_session: AsyncSession,
) -> None:
    """A checklist that says "Connect AWS: connect your AWS account" has told
    the reader nothing they did not get from the heading.
    """
    steps = await setup_progress(db_session, await _project(db_session))

    for step in steps:
        assert step.why
        assert step.why.lower() != step.title.lower()
        assert step.href.startswith("/")


# ------------------------------------------------------------- each step ---


async def test_an_api_key_ticks_the_first_step(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    await create_api_key(db_session, project_id=project_id, name="prod")

    assert "Create an API key" in _done(await setup_progress(db_session, project_id))


async def test_a_revoked_key_does_not_count(db_session: AsyncSession) -> None:
    """The reason this is read rather than recorded. A project whose only key
    was revoked has not still "created an API key" in any sense that helps -
    it cannot send.
    """
    project_id = await _project(db_session)
    issued = await create_api_key(db_session, project_id=project_id, name="prod")
    # Set directly rather than through `revoke_api_key`, which also needs Redis
    # to evict the auth cache. What is under test is the checklist's reading of
    # the row, not revocation.
    issued.api_key.revoked_at = utcnow()
    await db_session.flush()

    assert "Create an API key" not in _done(await setup_progress(db_session, project_id))


async def test_a_sent_message_ticks_the_second_step(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    db_session.add(
        Email(
            project_id=project_id,
            from_address="hello@example.com",
            to_addresses=["user@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Hello",
            text_body="Hello",
            status=EmailStatus.SENT.value,
        )
    )
    await db_session.flush()

    assert "Send a test message" in _done(await setup_progress(db_session, project_id))


async def test_a_connected_account_ticks_the_third_step(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    db_session.add(
        AWSConnection(
            project_id=project_id,
            region=REGION,
            aws_account_id=ACCOUNT_ID,
            status=ConnectionStatus.CONNECTED.value,
            aws_access_key_id=FAKE_CREDENTIALS.access_key_id,
            aws_secret_access_key_encrypted=encrypt_secret_access_key(
                FAKE_CREDENTIALS.secret_access_key, secret_key=TEST_SECRET_KEY
            ),
        )
    )
    await db_session.flush()

    assert "Connect an AWS account" in _done(await setup_progress(db_session, project_id))


async def test_a_broken_connection_does_not_count(db_session: AsyncSession) -> None:
    """Every connection made before Phase 14 is exactly this: a row that exists
    and cannot send. Telling somebody they had connected AWS would send them
    looking for the problem everywhere except where it is.
    """
    project_id = await _project(db_session)
    db_session.add(
        AWSConnection(
            project_id=project_id,
            region=REGION,
            aws_account_id=ACCOUNT_ID,
            status=ConnectionStatus.ERROR.value,
        )
    )
    await db_session.flush()

    assert "Connect an AWS account" not in _done(await setup_progress(db_session, project_id))


async def test_an_unverified_identity_does_not_count(db_session: AsyncSession) -> None:
    """Adding a domain is not verifying one. SES refuses to send from an
    identity still waiting on DNS, so a tick here would be a lie that costs
    somebody their first real send.
    """
    project_id = await _project(db_session)
    db_session.add(
        Identity(
            project_id=project_id,
            value="example.com",
            type=IdentityType.DOMAIN.value,
            region=REGION,
            verification_status=VerificationStatus.PENDING.value,
            dkim_tokens=[],
        )
    )
    await db_session.flush()

    assert "Verify a sender" not in _done(await setup_progress(db_session, project_id))


async def test_a_verified_identity_ticks_the_last_step(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    db_session.add(
        Identity(
            project_id=project_id,
            value="example.com",
            type=IdentityType.DOMAIN.value,
            region=REGION,
            verification_status=VerificationStatus.SUCCESS.value,
            dkim_tokens=[],
        )
    )
    await db_session.flush()

    assert "Verify a sender" in _done(await setup_progress(db_session, project_id))


# -------------------------------------------------------------- finished ---


async def test_the_list_is_only_complete_when_all_four_are(
    db_session: AsyncSession,
) -> None:
    """Not the first few. A project that can send through SES but has verified
    no sender is the case where the last step is the one that matters, and
    hiding the list at three of four would hide exactly that.
    """
    project_id = await _project(db_session)
    await create_api_key(db_session, project_id=project_id, name="prod")
    db_session.add(
        AWSConnection(
            project_id=project_id,
            region=REGION,
            aws_account_id=ACCOUNT_ID,
            status=ConnectionStatus.CONNECTED.value,
            aws_access_key_id=FAKE_CREDENTIALS.access_key_id,
            aws_secret_access_key_encrypted=encrypt_secret_access_key(
                FAKE_CREDENTIALS.secret_access_key, secret_key=TEST_SECRET_KEY
            ),
        )
    )
    await db_session.flush()

    assert is_complete(await setup_progress(db_session, project_id)) is False


async def test_another_projects_progress_is_not_borrowed(
    db_session: AsyncSession,
) -> None:
    """Two projects on one instance set up separately. Counting across them
    would tell a brand new project it had already done everything.
    """
    mine = await _project(db_session, owner="one@example.com")
    theirs = await _project(db_session, owner="two@example.com")
    await create_api_key(db_session, project_id=theirs, name="prod")

    assert _done(await setup_progress(db_session, mine)) == []


# ------------------------------------------------------------------ page ---


async def test_the_checklist_is_on_the_overview(signed_in_client: AsyncClient) -> None:
    """The first thing on a new install, above the metrics - which at that
    point are six zeroes.
    """
    page = await signed_in_client.get("/")

    assert "Finish setting up" in page.text
    assert "Create an API key" in page.text


async def test_the_checklist_goes_away_when_it_is_done(
    signed_in_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """It disappears rather than being dismissible. A checklist you can hide
    while it is unfinished is one that stops meaning anything.
    """
    from seskit_core.models import Project
    from sqlalchemy import select

    project_id = await db_session.scalar(select(Project.id))
    await create_api_key(db_session, project_id=str(project_id), name="prod")
    db_session.add(
        Email(
            project_id=project_id,
            from_address="hello@example.com",
            to_addresses=["user@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Hello",
            text_body="Hello",
            status=EmailStatus.SENT.value,
        )
    )
    db_session.add(
        AWSConnection(
            project_id=project_id,
            region=REGION,
            aws_account_id=ACCOUNT_ID,
            status=ConnectionStatus.CONNECTED.value,
            aws_access_key_id=FAKE_CREDENTIALS.access_key_id,
            aws_secret_access_key_encrypted=encrypt_secret_access_key(
                FAKE_CREDENTIALS.secret_access_key, secret_key=TEST_SECRET_KEY
            ),
        )
    )
    db_session.add(
        Identity(
            project_id=project_id,
            value="example.com",
            type=IdentityType.DOMAIN.value,
            region=REGION,
            verification_status=VerificationStatus.SUCCESS.value,
            dkim_tokens=[],
        )
    )
    await db_session.commit()

    page = await signed_in_client.get("/")

    assert "Finish setting up" not in page.text
