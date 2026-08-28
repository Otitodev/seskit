"""Translating SES identity responses.

The mapping is where this phase can go quietly wrong: SES reports verification
through different fields depending on the call, and a misreading produces a
domain that looks failed when it is merely new. moto does not implement these
calls usefully either, so a fake client stands in - the same division as
`test_ses_provider.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import IdentityType, VerificationStatus
from seskit_provider_aws_ses import SESProvider
from seskit_provider_aws_ses.identities import to_identity_status

REGION = "us-east-1"
DOMAIN = "example.com"
ADDRESS = "someone@example.com"
TOKENS = ["tok1", "tok2", "tok3"]

CREATE_DOMAIN_RESPONSE: dict[str, Any] = {
    "IdentityType": "DOMAIN",
    "VerifiedForSendingStatus": False,
    "DkimAttributes": {"SigningEnabled": True, "Status": "PENDING", "Tokens": TOKENS},
}

GET_DOMAIN_VERIFIED: dict[str, Any] = {
    "IdentityType": "DOMAIN",
    "VerificationStatus": "SUCCESS",
    "VerifiedForSendingStatus": True,
    "DkimAttributes": {"SigningEnabled": True, "Status": "SUCCESS", "Tokens": TOKENS},
    "MailFromAttributes": {"MailFromDomain": "mail.example.com", "MailFromDomainStatus": "SUCCESS"},
}

CREATE_ADDRESS_RESPONSE: dict[str, Any] = {
    "IdentityType": "EMAIL_ADDRESS",
    "VerifiedForSendingStatus": False,
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "CreateEmailIdentity")


class FakeSESClient:
    """Stands in for the boto3 sesv2 client."""

    def __init__(
        self,
        create: dict[str, Any] | None = None,
        get: dict[str, Any] | None = None,
        create_error: Exception | None = None,
        get_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self._create = create or CREATE_DOMAIN_RESPONSE
        self._get = get or GET_DOMAIN_VERIFIED
        self._create_error = create_error
        self._get_error = get_error
        self._delete_error = delete_error
        self.deleted: list[str] = []

    def create_email_identity(self, EmailIdentity: str) -> dict[str, Any]:
        if self._create_error is not None:
            raise self._create_error
        return self._create

    def get_email_identity(self, EmailIdentity: str) -> dict[str, Any]:
        if self._get_error is not None:
            raise self._get_error
        return self._get

    def delete_email_identity(self, EmailIdentity: str) -> dict[str, Any]:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(EmailIdentity)
        return {}


def _provider(monkeypatch: pytest.MonkeyPatch, client: FakeSESClient) -> SESProvider:
    provider = SESProvider(REGION)
    monkeypatch.setattr(provider._session, "client", lambda *a, **kw: client)
    return provider


# ----------------------------------------------------------------- mapping ---


def test_a_new_domain_is_pending_not_failed() -> None:
    """The bug this guards against.

    CreateEmailIdentity carries no VerificationStatus, only the boolean
    VerifiedForSendingStatus - which is false for every brand new identity.
    Reading that as a status would render every freshly added domain as failed.
    """
    status = to_identity_status(DOMAIN, CREATE_DOMAIN_RESPONSE, fallback_type=IdentityType.DOMAIN)

    assert status.verification_status is VerificationStatus.PENDING
    assert status.is_verified is False


def test_a_verified_domain_reads_its_verification_status() -> None:
    status = to_identity_status(DOMAIN, GET_DOMAIN_VERIFIED, fallback_type=IdentityType.DOMAIN)

    assert status.verification_status is VerificationStatus.SUCCESS
    assert status.dkim_status is VerificationStatus.SUCCESS
    assert status.mail_from_status is VerificationStatus.SUCCESS


def test_dkim_tokens_are_carried_through() -> None:
    status = to_identity_status(DOMAIN, CREATE_DOMAIN_RESPONSE, fallback_type=IdentityType.DOMAIN)

    assert status.dkim_tokens == TOKENS


def test_an_address_gets_no_dkim_or_mail_from() -> None:
    """Inapplicable, not pending - which is why those fields are optional."""
    status = to_identity_status(
        ADDRESS, CREATE_ADDRESS_RESPONSE, fallback_type=IdentityType.EMAIL_ADDRESS
    )

    assert status.identity_type is IdentityType.EMAIL_ADDRESS
    assert status.dkim_status is None
    assert status.mail_from_status is None
    assert status.dkim_tokens == []


def test_a_domain_without_custom_mail_from_is_not_started() -> None:
    """Absent MailFromAttributes means the SES default is in use, which is not
    a pending anything.
    """
    status = to_identity_status(
        DOMAIN,
        {"IdentityType": "DOMAIN", "VerificationStatus": "SUCCESS", "DkimAttributes": {}},
        fallback_type=IdentityType.DOMAIN,
    )

    assert status.mail_from_status is VerificationStatus.NOT_STARTED


def test_ses_reported_type_beats_the_caller_guess() -> None:
    """A user who types a domain into the address field should get what SES
    actually created, not what the form assumed.
    """
    status = to_identity_status(
        DOMAIN, CREATE_DOMAIN_RESPONSE, fallback_type=IdentityType.EMAIL_ADDRESS
    )

    assert status.identity_type is IdentityType.DOMAIN


def test_a_managed_domain_behaves_as_a_domain() -> None:
    status = to_identity_status(
        DOMAIN,
        {"IdentityType": "MANAGED_DOMAIN", "VerificationStatus": "SUCCESS"},
        fallback_type=IdentityType.DOMAIN,
    )

    assert status.identity_type is IdentityType.DOMAIN


def test_an_unrecognised_status_becomes_pending_not_a_crash() -> None:
    """A status we have never seen must land somewhere deliberate. Pending is
    the honest reading: we asked and do not have an answer.
    """
    status = to_identity_status(
        DOMAIN,
        {"IdentityType": "DOMAIN", "VerificationStatus": "SOMETHING_NEW"},
        fallback_type=IdentityType.DOMAIN,
    )

    assert status.verification_status is VerificationStatus.PENDING


# --------------------------------------------------------------- adoption ---


async def test_creating_an_existing_identity_adopts_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refcount case, at the adapter level.

    A second project adding a domain the first already verified must inherit
    that state - not be refused, and certainly not be shown DNS records that are
    already published.
    """
    client = FakeSESClient(
        create_error=_client_error("AlreadyExistsException"), get=GET_DOMAIN_VERIFIED
    )

    status = await _provider(monkeypatch, client).create_identity(DOMAIN, IdentityType.DOMAIN)

    assert status.verification_status is VerificationStatus.SUCCESS


async def test_other_create_failures_still_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient(create_error=_client_error("AccessDeniedException"))

    with pytest.raises(APIError) as caught:
        await _provider(monkeypatch, client).create_identity(DOMAIN, IdentityType.DOMAIN)

    assert caught.value.error_type is ErrorType.AUTHORIZATION_FAILED
    assert "ses:CreateEmailIdentity" in caught.value.message


# --------------------------------------------------------------- deletion ---


async def test_deleting_calls_ses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient()

    await _provider(monkeypatch, client).delete_identity(DOMAIN)

    assert client.deleted == [DOMAIN]


async def test_deleting_something_already_gone_is_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller wanted it absent and it is. Raising would leave a SESKit row
    that can never be cleaned up.
    """
    client = FakeSESClient(delete_error=_client_error("NotFoundException"))

    await _provider(monkeypatch, client).delete_identity(DOMAIN)


async def test_a_denied_delete_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient(delete_error=_client_error("AccessDeniedException"))

    with pytest.raises(APIError):
        await _provider(monkeypatch, client).delete_identity(DOMAIN)


# ------------------------------------------------------------------ errors ---


async def test_a_missing_identity_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSESClient(get_error=_client_error("NotFoundException"))

    with pytest.raises(APIError) as caught:
        await _provider(monkeypatch, client).get_identity_status(DOMAIN)

    assert caught.value.error_type is ErrorType.NOT_FOUND
