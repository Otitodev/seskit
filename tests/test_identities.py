"""Identities: domains and email addresses SES has been asked to verify.

The model half. Nothing here talks to AWS - what is checked is the vocabulary
the rest of the phase depends on: that an address is not treated as a domain,
and that the DNS records shown to a user are built correctly from the tokens SES
returned.
"""

from __future__ import annotations

import pytest
from seskit_core.models import Identity
from seskit_core.providers import IdentityStatus, IdentityType, VerificationStatus

DOMAIN = "example.com"
ADDRESS = "someone@example.com"
REGION = "us-east-1"
TOKENS = ["tok1abc", "tok2def", "tok3ghi"]


def _domain(**kwargs: object) -> Identity:
    defaults: dict[str, object] = {
        "project_id": "proj_01TEST",
        "identity_type": IdentityType.DOMAIN.value,
        "value": DOMAIN,
        "region": REGION,
        "verification_status": VerificationStatus.PENDING.value,
        "dkim_status": VerificationStatus.PENDING.value,
        "dkim_tokens": TOKENS,
    }
    defaults.update(kwargs)
    return Identity(**defaults)


def _address(**kwargs: object) -> Identity:
    defaults: dict[str, object] = {
        "project_id": "proj_01TEST",
        "identity_type": IdentityType.EMAIL_ADDRESS.value,
        "value": ADDRESS,
        "region": REGION,
        "verification_status": VerificationStatus.PENDING.value,
        "dkim_tokens": [],
    }
    defaults.update(kwargs)
    return Identity(**defaults)


# ------------------------------------------------------------------- type ---


def test_a_domain_knows_it_is_a_domain() -> None:
    assert _domain().is_domain is True
    assert _domain().type is IdentityType.DOMAIN


def test_an_address_is_not_a_domain() -> None:
    """The distinction the whole phase hangs on. An address cannot have DKIM and
    must not be asked to publish anything.
    """
    assert _address().is_domain is False
    assert _address().type is IdentityType.EMAIL_ADDRESS


# -------------------------------------------------------------- dns records ---


def test_a_domain_renders_one_cname_per_token() -> None:
    records = _domain().dns_records

    assert len(records) == 3
    assert {record.record_type for record in records} == {"CNAME"}


def test_the_cname_matches_the_shape_ses_expects() -> None:
    """``{token}._domainkey.{domain}`` -> ``{token}.dkim.amazonses.com``.

    Getting this wrong produces a domain that never verifies and an error
    message that says nothing useful, so it is worth pinning exactly.
    """
    first = _domain().dns_records[0]

    assert first.name == f"{TOKENS[0]}._domainkey.{DOMAIN}"
    assert first.value == f"{TOKENS[0]}.dkim.amazonses.com"


def test_an_address_has_no_records_to_publish() -> None:
    """It is verified by clicking a link, which is the entire point of offering
    it - no DNS, no registrar, no waiting.
    """
    assert _address().dns_records == []


def test_a_domain_with_no_tokens_yet_renders_nothing() -> None:
    """Between creation and SES returning tokens there is a moment with none.
    The page must show an empty list rather than a malformed record.
    """
    assert _domain(dkim_tokens=[]).dns_records == []


# ---------------------------------------------------------------- verified ---


def test_only_success_counts_as_verified() -> None:
    for status in (
        VerificationStatus.PENDING,
        VerificationStatus.FAILED,
        VerificationStatus.TEMPORARY_FAILURE,
        VerificationStatus.NOT_STARTED,
    ):
        assert _domain(verification_status=status.value).is_verified is False

    assert _domain(verification_status=VerificationStatus.SUCCESS.value).is_verified is True


def test_verification_does_not_require_dkim() -> None:
    """An unsigned message still sends. Requiring DKIM here would block a user
    whose domain is verified but whose DKIM records have not propagated.
    """
    identity = _domain(
        verification_status=VerificationStatus.SUCCESS.value,
        dkim_status=VerificationStatus.PENDING.value,
    )

    assert identity.is_verified is True


# ------------------------------------------------------------ inapplicable ---


def test_an_address_leaves_dkim_null_rather_than_pending() -> None:
    """NULL means inapplicable; "pending" would promise something that can never
    happen, and the UI would render a row that never resolves.
    """
    identity = _address()

    assert identity.dkim_status is None
    assert identity.mail_from_status is None


# ------------------------------------------------------------ provider type ---


def test_identity_status_reports_verification() -> None:
    status = IdentityStatus(
        value=DOMAIN,
        identity_type=IdentityType.DOMAIN,
        verification_status=VerificationStatus.SUCCESS,
    )

    assert status.is_verified is True


def test_identity_status_defaults_dkim_to_inapplicable() -> None:
    """The provider type carries the same distinction as the row, so an adapter
    that simply does not know cannot accidentally claim "not started".
    """
    status = IdentityStatus(
        value=ADDRESS,
        identity_type=IdentityType.EMAIL_ADDRESS,
        verification_status=VerificationStatus.PENDING,
    )

    assert status.dkim_status is None


def test_identity_status_is_immutable() -> None:
    status = IdentityStatus(
        value=DOMAIN,
        identity_type=IdentityType.DOMAIN,
        verification_status=VerificationStatus.PENDING,
    )

    with pytest.raises(AttributeError):
        status.verification_status = VerificationStatus.SUCCESS  # type: ignore[misc]


# =========================================================== service layer ===
#
# Real database, fake provider. The fake keeps identities in one shared store
# keyed by value rather than per project, because that is exactly the fact the
# refcount exists to handle.


from datetime import timedelta

from fakes.ses import FakeProviderFactory, denied
from redis.asyncio import Redis
from seskit_core.errors import APIError
from seskit_core.models import Project, utcnow
from seskit_core.services import (
    add_identity,
    classify,
    count_other_references,
    create_project,
    identities_due,
    is_recheck_due,
    list_identities,
    refresh_identity,
    register_user,
    remove_identity,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
UNVERIFIED = 6 * 60 * 60
VERIFIED = 30 * 24 * 60 * 60


async def _make_project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return str(project.id)


# --------------------------------------------------------------- classify ---


def test_a_domain_is_recognised() -> None:
    assert classify("Example.COM") == ("example.com", IdentityType.DOMAIN)


def test_an_address_is_recognised() -> None:
    assert classify("  SomeOne@Example.com ") == (ADDRESS, IdentityType.EMAIL_ADDRESS)


def test_values_are_lower_cased() -> None:
    """A duplicate differing only in case would slip past the uniqueness
    constraint and become a second row pointing at one SES identity.
    """
    value, _ = classify("EXAMPLE.com")

    assert value == "example.com"


def test_obvious_nonsense_is_refused_with_a_useful_message() -> None:
    for bad in ("", "   ", "no-dot", "@example.com", "you@", "has space.com", "x@y"):
        with pytest.raises(APIError) as caught:
            classify(bad)
        assert "example.com" in caught.value.message


# ----------------------------------------------------------------- adding ---


async def test_adding_a_domain_stores_its_tokens(db_session: AsyncSession) -> None:
    project_id = await _make_project(db_session)

    identity = await add_identity(
        db_session,
        FakeProviderFactory(),
        project_id=project_id,
        value=DOMAIN,
        region=REGION,
    )

    assert identity.is_domain is True
    assert len(identity.dkim_tokens) == 3
    assert len(identity.dns_records) == 3
    assert identity.verification_status == VerificationStatus.PENDING.value


async def test_adding_an_address_stores_no_tokens(db_session: AsyncSession) -> None:
    """The zero-DNS path: nothing to publish, nothing to copy."""
    project_id = await _make_project(db_session)

    identity = await add_identity(
        db_session,
        FakeProviderFactory(),
        project_id=project_id,
        value=ADDRESS,
        region=REGION,
    )

    assert identity.is_domain is False
    assert identity.dns_records == []
    assert identity.dkim_status is None


async def test_adding_twice_updates_rather_than_duplicating(db_session: AsyncSession) -> None:
    """A user pressing Add twice should not meet a constraint error."""
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()

    first = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )
    second = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )

    assert first.id == second.id
    assert len(await list_identities(db_session, project_id)) == 1


async def test_a_second_project_adopts_an_already_verified_domain(
    db_session: AsyncSession,
) -> None:
    """Otherwise the second project is told to publish records that are already
    published, and waits for a verification that has already happened.
    """
    mine = await _make_project(db_session, email="me@example.com")
    theirs = await _make_project(db_session, email="them@example.com")
    factory = FakeProviderFactory()

    await add_identity(db_session, factory, project_id=mine, value=DOMAIN, region=REGION)
    factory.provider.mark_verified(DOMAIN)

    adopted = await add_identity(
        db_session, factory, project_id=theirs, value=DOMAIN, region=REGION
    )

    assert adopted.is_verified is True


# --------------------------------------------------------------- refcount ---


async def test_removing_one_of_two_references_leaves_ses_alone(
    db_session: AsyncSession,
) -> None:
    """The test that matters.

    If either project could delete the shared SES identity, the other would
    silently stop sending with nothing on screen to explain why.
    """
    mine = await _make_project(db_session, email="me@example.com")
    theirs = await _make_project(db_session, email="them@example.com")
    factory = FakeProviderFactory()

    await add_identity(db_session, factory, project_id=mine, value=DOMAIN, region=REGION)
    ours = await add_identity(db_session, factory, project_id=theirs, value=DOMAIN, region=REGION)

    deleted_in_ses = await remove_identity(db_session, factory, ours)

    assert deleted_in_ses is False
    assert factory.provider.delete_calls == 0
    assert DOMAIN in factory.provider.identities


async def test_removing_the_last_reference_deletes_in_ses(db_session: AsyncSession) -> None:
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()
    identity = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )

    deleted_in_ses = await remove_identity(db_session, factory, identity)

    assert deleted_in_ses is True
    assert factory.provider.delete_calls == 1
    assert DOMAIN not in factory.provider.identities


async def test_the_same_domain_in_two_regions_is_two_identities(
    db_session: AsyncSession,
) -> None:
    """SES scopes an identity to a region, so these do not reference each other
    and removing one must not spare the other.
    """
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()

    first = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )
    await add_identity(db_session, factory, project_id=project_id, value=DOMAIN, region="eu-west-1")

    assert await count_other_references(db_session, first) == 0


# ---------------------------------------------------------------- refresh ---


async def test_refresh_reads_the_current_state(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()
    identity = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )
    factory.provider.mark_verified(DOMAIN)

    await refresh_identity(db_session, redis_client, factory, identity, interval_seconds=60)

    assert identity.is_verified is True


async def test_a_second_refresh_inside_the_interval_does_not_call_ses(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Holding the button must not spend the project SES quota."""
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()
    identity = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )

    await refresh_identity(db_session, redis_client, factory, identity, interval_seconds=60)
    calls = factory.provider.get_calls
    await refresh_identity(db_session, redis_client, factory, identity, interval_seconds=60)

    assert factory.provider.get_calls == calls


async def test_a_failed_check_is_recorded_not_raised(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """The scheduled job runs over many identities; one unreachable domain must
    not stop the rest.
    """
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()
    identity = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )
    factory.provider.error = denied()

    await refresh_identity(db_session, redis_client, factory, identity, interval_seconds=0)

    assert identity.last_error is not None


# -------------------------------------------------------------------- due ---


def test_a_never_checked_identity_is_always_due() -> None:
    due = is_recheck_due(
        _domain(last_checked_at=None),
        unverified_seconds=UNVERIFIED,
        verified_seconds=VERIFIED,
    )

    assert due is True


def test_an_unverified_identity_is_due_after_its_interval() -> None:
    now = utcnow()
    recent = _domain(last_checked_at=now - timedelta(hours=1))
    stale = _domain(last_checked_at=now - timedelta(hours=7))

    assert (
        is_recheck_due(recent, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED, now=now)
        is False
    )
    assert (
        is_recheck_due(stale, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED, now=now)
        is True
    )


def test_a_verified_identity_waits_far_longer() -> None:
    """Not never, though: this is what catches a DKIM record deleted months
    after setup.
    """
    now = utcnow()
    verified = VerificationStatus.SUCCESS.value
    week_old = _domain(last_checked_at=now - timedelta(days=7), verification_status=verified)
    ancient = _domain(last_checked_at=now - timedelta(days=31), verification_status=verified)

    assert (
        is_recheck_due(week_old, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED, now=now)
        is False
    )
    assert (
        is_recheck_due(ancient, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED, now=now)
        is True
    )


async def test_due_selects_only_what_needs_checking(db_session: AsyncSession) -> None:
    project_id = await _make_project(db_session)
    factory = FakeProviderFactory()
    fresh = await add_identity(
        db_session, factory, project_id=project_id, value=DOMAIN, region=REGION
    )
    stale = await add_identity(
        db_session, factory, project_id=project_id, value="other.example", region=REGION
    )
    stale.last_checked_at = utcnow() - timedelta(days=1)
    await db_session.flush()

    due = await identities_due(db_session, unverified_seconds=UNVERIFIED, verified_seconds=VERIFIED)

    ids = {identity.id for identity in due}
    assert stale.id in ids
    assert fresh.id not in ids


# ------------------------------------------------------------- boundaries ---


async def test_deleting_a_project_deletes_its_identities(db_session: AsyncSession) -> None:
    project_id = await _make_project(db_session)
    await add_identity(
        db_session, FakeProviderFactory(), project_id=project_id, value=DOMAIN, region=REGION
    )

    project = await db_session.get(Project, project_id)
    assert project is not None
    await db_session.delete(project)
    await db_session.flush()

    assert await list_identities(db_session, project_id) == []
