"""Leaving the SES sandbox from the dashboard (§31 Phase 16): the gates, and
the request that only goes out once they pass.

AWS reviews a production access request by hand. Its form asks the sender to
attest to a bounce and complaint process and its docs say a verified domain
is what gets a request approved quickly. SESKit holds the state that answers
both, so the service checks them and refuses to file a request that AWS would
turn down. What these tests pin is that the refusal is real - the provider is
never called - and that each gate is judged on the right evidence.
"""

from __future__ import annotations

import pytest
from fakes.ses import ACCOUNT_ID, TEST_SECRET_KEY, FakeProviderFactory, connect_project, denied
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import (
    AWSConnection,
    Email,
    EmailStatus,
    Identity,
    utcnow,
)
from seskit_core.providers import (
    ContactLanguage,
    IdentityType,
    MailType,
    ReviewStatus,
    VerificationStatus,
)
from seskit_core.services import (
    MAX_CONTACTS,
    create_project,
    describe_use_case,
    production_access_readiness,
    register_user,
    request_production_access,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
DOMAIN = "otito.site"
WEBSITE = "https://otito.site"
CONTACT = "owner@example.com"


# ----------------------------------------------------------------- setup ---


async def _connected(
    session: AsyncSession, *, owner: str = CONTACT, region: str = REGION
) -> AWSConnection:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    connection: AWSConnection = await connect_project(session, project.id, region=region)
    return connection


async def _verified(
    session: AsyncSession,
    project_id: str,
    value: str = DOMAIN,
    *,
    identity_type: IdentityType = IdentityType.DOMAIN,
    status: VerificationStatus = VerificationStatus.SUCCESS,
    region: str = REGION,
) -> None:
    session.add(
        Identity(
            project_id=project_id,
            value=value,
            identity_type=identity_type.value,
            region=region,
            verification_status=status.value,
            dkim_tokens=[],
        )
    )
    await session.flush()


def _events_on(connection: AWSConnection) -> None:
    connection.configuration_set = "seskit-events"
    connection.event_topic_arn = f"arn:aws:sns:{connection.region}:{ACCOUNT_ID}:seskit-events"


async def _delivered(session: AsyncSession, project_id: str, *, delivered: bool = True) -> None:
    session.add(
        Email(
            project_id=project_id,
            from_address=f"hello@{DOMAIN}",
            to_addresses=["you@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Test",
            text_body="Hello",
            status=EmailStatus.SENT.value,
            provider="ses",
            provider_message_id="m-1",
            delivered_at=utcnow() if delivered else None,
        )
    )
    await session.flush()


async def _ready(session: AsyncSession) -> AWSConnection:
    """Every gate passed."""
    connection = await _connected(session)
    await _verified(session, connection.project_id)
    _events_on(connection)
    await _delivered(session, connection.project_id)
    await session.flush()
    return connection


async def _request(
    session: AsyncSession,
    factory: FakeProviderFactory,
    connection: AWSConnection,
    **overrides: object,
) -> object:
    fields: dict[str, object] = {
        "mail_type": MailType.TRANSACTIONAL,
        "website_url": WEBSITE,
        "contact_addresses": [CONTACT],
        "acknowledged": True,
        "secret_key": TEST_SECRET_KEY,
    }
    fields.update(overrides)
    return await request_production_access(session, factory, connection, **fields)  # type: ignore[arg-type]


# ------------------------------------------------------------------ gates ---


async def test_a_fresh_connection_passes_no_gate(db_session: AsyncSession) -> None:
    connection = await _connected(db_session)

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.ready is False
    assert readiness.unmet == [
        "a verified domain",
        "delivery event reporting",
        "a delivered message",
    ]


async def test_every_gate_passed_is_ready(db_session: AsyncSession) -> None:
    connection = await _ready(db_session)

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.ready is True
    assert readiness.verified_domain == DOMAIN
    assert readiness.delivered_count == 1
    assert readiness.unmet == []


async def test_a_verified_address_is_not_a_verified_domain(db_session: AsyncSession) -> None:
    """SES lets an address send. AWS says a domain is what gets the request
    approved, so an address-only account is told to verify one.
    """
    connection = await _connected(db_session)
    await _verified(
        db_session,
        connection.project_id,
        "me@example.com",
        identity_type=IdentityType.EMAIL_ADDRESS,
    )

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.has_verified_domain is False


async def test_a_pending_domain_does_not_count(db_session: AsyncSession) -> None:
    connection = await _connected(db_session)
    await _verified(db_session, connection.project_id, status=VerificationStatus.PENDING)

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.has_verified_domain is False


async def test_a_domain_in_another_region_does_not_count(db_session: AsyncSession) -> None:
    """The sandbox is per region, and so are identities."""
    connection = await _connected(db_session)
    await _verified(db_session, connection.project_id, region="eu-west-2")

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.has_verified_domain is False


async def test_a_sibling_project_on_the_same_account_counts(db_session: AsyncSession) -> None:
    """Two projects, one AWS account and region. SES sees one account; a
    domain the other project verified is verified for this one too, and a
    message it delivered went through the same pipeline.
    """
    mine = await _connected(db_session, owner="a@example.com")
    sibling = await _connected(db_session, owner="b@example.com")
    await _verified(db_session, sibling.project_id)
    await _delivered(db_session, sibling.project_id)

    readiness = await production_access_readiness(db_session, mine)

    assert readiness.verified_domain == DOMAIN
    assert readiness.delivered_count == 1


async def test_a_sent_but_undelivered_message_does_not_count(db_session: AsyncSession) -> None:
    """Sent proves the key works. Delivered proves the event came back, which
    is the process AWS is asking about.
    """
    connection = await _connected(db_session)
    await _delivered(db_session, connection.project_id, delivered=False)

    readiness = await production_access_readiness(db_session, connection)

    assert readiness.has_delivered is False


# ---------------------------------------------------------------- request ---


async def test_the_request_carries_the_form_and_the_generated_use_case(
    db_session: AsyncSession,
) -> None:
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)

    await _request(
        db_session,
        factory,
        connection,
        contact_addresses=[CONTACT, " dev@example.com "],
        contact_language=ContactLanguage.JA,
    )

    (sent,) = factory.provider.production_access_requests
    assert sent.mail_type is MailType.TRANSACTIONAL
    assert sent.website_url == WEBSITE
    assert sent.contact_addresses == (CONTACT, "dev@example.com")
    assert sent.contact_language is ContactLanguage.JA
    assert DOMAIN in sent.use_case_description
    assert "suppressed automatically" in sent.use_case_description


async def test_the_request_is_recorded_as_pending(db_session: AsyncSession) -> None:
    """Immediately, not on the next refresh: the page must show the wait as
    soon as the button is pressed, and SES refuses a second request meanwhile.
    """
    connection = await _ready(db_session)

    await _request(db_session, FakeProviderFactory(sandbox=True), connection)

    assert connection.review_status == ReviewStatus.PENDING.value
    assert connection.production_requested_at is not None
    assert connection.production_access_pending is True


async def test_an_unmet_gate_refuses_before_aws_is_asked(db_session: AsyncSession) -> None:
    """The finding this phase exists for. A request AWS turns down costs the
    user a day and a Support case; the refusal names what is left instead.
    """
    connection = await _connected(db_session)
    await _verified(db_session, connection.project_id)
    factory = FakeProviderFactory(sandbox=True)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection)

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert "delivery event reporting" in caught.value.message
    assert "a delivered message" in caught.value.message
    assert "a verified domain" not in caught.value.message
    assert factory.provider.production_access_requests == []
    assert connection.review_status is None


async def test_a_request_already_under_review_is_not_repeated(db_session: AsyncSession) -> None:
    connection = await _ready(db_session)
    connection.review_status = ReviewStatus.PENDING.value
    factory = FakeProviderFactory(sandbox=True)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection)

    assert "still reviewing" in caught.value.message
    assert factory.provider.production_access_requests == []


async def test_an_account_already_in_production_has_nothing_to_request(
    db_session: AsyncSession,
) -> None:
    connection = await _ready(db_session)
    connection.sandbox = False
    factory = FakeProviderFactory(sandbox=False)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection)

    assert "already has production access" in caught.value.message
    assert factory.provider.production_access_requests == []


@pytest.mark.parametrize("url", ["", "otito.site", "ftp://otito.site", "https://", "https://nodot"])
async def test_a_website_url_that_is_not_one_is_refused(db_session: AsyncSession, url: str) -> None:
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection, website_url=url)

    assert "website URL" in caught.value.message
    assert factory.provider.production_access_requests == []


async def test_no_contact_address_is_refused(db_session: AsyncSession) -> None:
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection, contact_addresses=["", "  "])

    assert "contact email address" in caught.value.message
    assert factory.provider.production_access_requests == []


async def test_more_contacts_than_aws_allows_is_refused(db_session: AsyncSession) -> None:
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)
    too_many = [f"c{i}@example.com" for i in range(MAX_CONTACTS + 1)]

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection, contact_addresses=too_many)

    assert str(MAX_CONTACTS) in caught.value.message
    assert factory.provider.production_access_requests == []


async def test_without_the_acknowledgement_nothing_is_sent(db_session: AsyncSession) -> None:
    """AWS's checkbox, and SESKit does not tick it on the user's behalf."""
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection, acknowledged=False)

    assert "acknowledgement" in caught.value.message
    assert factory.provider.production_access_requests == []


async def test_a_refusal_from_aws_records_no_request(db_session: AsyncSession) -> None:
    """A key from the older documented policy lacks ses:PutAccountDetails.
    The error names it, and the row does not claim a request was made.
    """
    connection = await _ready(db_session)
    factory = FakeProviderFactory(sandbox=True)
    factory.provider.request_error = denied("ses:PutAccountDetails")

    with pytest.raises(APIError) as caught:
        await _request(db_session, factory, connection)

    assert "ses:PutAccountDetails" in caught.value.message
    assert connection.review_status is None
    assert connection.production_requested_at is None


# --------------------------------------------------------------- use case ---


def test_the_use_case_describes_the_process_a_reviewer_asks_about() -> None:
    from seskit_core.services import Readiness

    text = describe_use_case(
        MailType.MARKETING,
        Readiness(verified_domain=DOMAIN, events_enabled=True, delivered_count=3),
    )

    assert text.startswith("Marketing email from the verified domain otito.site")
    assert "Bounce and complaint" in text
    assert "3 message(s)" in text
