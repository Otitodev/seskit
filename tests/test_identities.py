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
