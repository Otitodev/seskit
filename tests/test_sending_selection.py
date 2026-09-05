"""Which provider carries a message (§8, §25).

The matrix, and particularly its awkward corner: a project that has connected
AWS but whose sender is not verified. Falling back to Mailpit there would report
a successful send while delivering to nobody, which is the worst of the four
possible behaviours and the easiest one to write by accident.
"""

from __future__ import annotations

import pytest
from fakes.ses import FakeProviderFactory
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import Email, EmailProvider
from seskit_core.services import (
    add_identity,
    choose_provider,
    connect_aws,
    create_project,
    register_user,
    sender_is_verified,
)
from seskit_core.services.identities import check_identity
from seskit_core.services.sending import to_outbound
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
DOMAIN = "example.com"
ADDRESS = "hello@example.com"
SENDER = "Acme <hello@example.com>"


async def _project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return str(project.id)


async def _verified(
    session: AsyncSession, factory: FakeProviderFactory, project_id: str, value: str
) -> None:
    identity = await add_identity(
        session, factory, project_id=project_id, value=value, region=REGION
    )
    factory.provider.mark_verified(value)
    await check_identity(session, factory, identity)


# ------------------------------------------------------------- no AWS yet ---


async def test_without_a_connection_it_uses_smtp(db_session: AsyncSession) -> None:
    """Rung zero of the friction ladder: a send works before AWS exists."""
    project_id = await _project(db_session)

    provider = await choose_provider(
        db_session, project_id=project_id, sender=SENDER, smtp_configured=True
    )

    assert provider is EmailProvider.SMTP


async def test_with_nothing_configured_it_says_what_to_do(db_session: AsyncSession) -> None:
    """A project that cannot send should be told how to make it able to, not
    handed a provider error from a server that was never configured.
    """
    project_id = await _project(db_session)

    with pytest.raises(APIError) as caught:
        await choose_provider(
            db_session, project_id=project_id, sender=SENDER, smtp_configured=False
        )

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert "SMTP_HOST" in caught.value.message


# ------------------------------------------------------------- AWS ready ---


async def test_a_verified_address_sends_through_ses(
    db_session: AsyncSession, redis_client: object, provider_factory: FakeProviderFactory
) -> None:
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project_id,
        region=REGION,
    )
    await _verified(db_session, provider_factory, project_id, ADDRESS)

    provider = await choose_provider(
        db_session, project_id=project_id, sender=SENDER, smtp_configured=True
    )

    assert provider is EmailProvider.SES


async def test_a_verified_domain_covers_any_address_on_it(
    db_session: AsyncSession, redis_client: object, provider_factory: FakeProviderFactory
) -> None:
    """This is what verifying a domain buys - otherwise every sending address
    would need verifying one at a time.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project_id,
        region=REGION,
    )
    await _verified(db_session, provider_factory, project_id, DOMAIN)

    provider = await choose_provider(
        db_session,
        project_id=project_id,
        sender="anything@example.com",
        smtp_configured=True,
    )

    assert provider is EmailProvider.SES


# --------------------------------------------------- the awkward corner ---


async def test_connected_but_unverified_is_refused_not_diverted(
    db_session: AsyncSession, redis_client: object, provider_factory: FakeProviderFactory
) -> None:
    """The test that matters.

    Once a project has connected AWS it has declared an intent to send for real.
    Quietly using Mailpit instead would report success while the message reached
    nobody - a silent failure that only surfaces when a customer asks where
    their email went.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project_id,
        region=REGION,
    )

    with pytest.raises(APIError) as caught:
        await choose_provider(
            db_session, project_id=project_id, sender=SENDER, smtp_configured=True
        )

    assert caught.value.error_type is ErrorType.DOMAIN_NOT_VERIFIED
    assert "hello@example.com" in caught.value.message


async def test_an_unverified_identity_does_not_count(
    db_session: AsyncSession, redis_client: object, provider_factory: FakeProviderFactory
) -> None:
    """Added is not verified. SES would refuse it, so we refuse it first and
    say something more useful than SES would.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,  # type: ignore[arg-type]
        provider_factory,
        project_id=project_id,
        region=REGION,
    )
    await add_identity(
        db_session, provider_factory, project_id=project_id, value=ADDRESS, region=REGION
    )

    with pytest.raises(APIError):
        await choose_provider(
            db_session, project_id=project_id, sender=SENDER, smtp_configured=True
        )


# ------------------------------------------------------------- boundaries ---


async def test_verification_does_not_leak_between_projects(
    db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """One project verifying a domain must not license another to send as it."""
    mine = await _project(db_session, email="me@example.com")
    theirs = await _project(db_session, email="them@example.com")
    await _verified(db_session, provider_factory, mine, DOMAIN)

    assert await sender_is_verified(db_session, project_id=mine, sender=SENDER) is True
    assert await sender_is_verified(db_session, project_id=theirs, sender=SENDER) is False


async def test_a_display_name_does_not_confuse_the_check(
    db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """§11's own example uses "Acme <hello@example.com>". Comparing the whole
    string against the identity would never match.
    """
    project_id = await _project(db_session)
    await _verified(db_session, provider_factory, project_id, ADDRESS)

    assert await sender_is_verified(db_session, project_id=project_id, sender=SENDER) is True


async def test_the_check_is_case_insensitive(
    db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    project_id = await _project(db_session)
    await _verified(db_session, provider_factory, project_id, DOMAIN)

    verified = await sender_is_verified(
        db_session, project_id=project_id, sender="Hello@EXAMPLE.com"
    )

    assert verified is True


async def test_a_lookalike_domain_does_not_qualify(
    db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """`notexample.com` must not match a verified `example.com` - a substring
    check here would let anyone send as a domain they nearly own.
    """
    project_id = await _project(db_session)
    await _verified(db_session, provider_factory, project_id, DOMAIN)

    verified = await sender_is_verified(
        db_session, project_id=project_id, sender="hi@notexample.com"
    )

    assert verified is False


async def test_a_subdomain_does_not_qualify_on_its_own(
    db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """SES treats mail.example.com as its own identity, so we do too. Assuming
    otherwise would send something SES then rejects.
    """
    project_id = await _project(db_session)
    await _verified(db_session, provider_factory, project_id, DOMAIN)

    verified = await sender_is_verified(
        db_session, project_id=project_id, sender="hi@mail.example.com"
    )

    assert verified is False


# ------------------------------------------------- the row as a provider sees it ---


def _row(**kwargs: object) -> Email:
    """An unsaved ``Email``, which is all ``to_outbound`` needs.

    No session: this is a pure translation from the row to the vocabulary a
    provider speaks, and giving it a database would hide that.
    """
    defaults: dict[str, object] = {
        "project_id": "proj_test",
        "from_address": "Acme <hello@example.com>",
        "to_addresses": ["user@example.com"],
        "cc_addresses": [],
        "bcc_addresses": [],
        "reply_to": [],
        "subject": "Welcome",
        "html_body": "<h1>Hello</h1>",
        "text_body": "Hello",
        "headers": {},
    }
    defaults.update(kwargs)
    return Email(**defaults)


def test_the_custom_headers_on_the_row_reach_the_provider() -> None:
    """The seam this whole commit exists for.

    `build_message` has always carried `OutboundEmail.headers` and had a test
    saying so. Nothing tested the step before it, so the API validated a
    caller's headers, answered 201 and dropped them, and both halves looked
    covered.
    """
    outbound = to_outbound(_row(headers={"X-Entity-Ref-Id": "order-1234"}))

    assert outbound.headers == {"X-Entity-Ref-Id": "order-1234"}


def test_a_row_written_before_the_column_existed_still_sends() -> None:
    """`headers` arrived after messages already existed.

    A row from before the migration reads back as NULL rather than `{}`, and a
    queued message that cannot be assembled is one nobody can retry.
    """
    row = _row()
    row.headers = None  # type: ignore[assignment]

    assert to_outbound(row).headers == {}


def test_the_row_is_translated_whole() -> None:
    """Guards the shape rather than one field: every recipient list the row
    carries has been dropped on this path at least once.
    """
    outbound = to_outbound(
        _row(
            cc_addresses=["cc@example.com"],
            bcc_addresses=["quiet@example.com"],
            reply_to=["reply@example.com"],
        )
    )

    assert outbound.to == ["user@example.com"]
    assert outbound.cc == ["cc@example.com"]
    assert outbound.bcc == ["quiet@example.com"]
    assert outbound.reply_to == ["reply@example.com"]
    assert outbound.subject == "Welcome"
