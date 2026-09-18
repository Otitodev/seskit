"""The SES adapter: response parsing and error normalisation.

No AWS and no moto here - a fake boto3 client is substituted for the session, so
every branch (sandbox on, sandbox off, each botocore failure) can be provoked on
demand. What this proves is the translation layer: AWS's vocabulary in, core's
vocabulary out, and nothing provider-shaped escaping in either direction.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from fakes.ses import FAKE_CREDENTIALS
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import (
    ContactLanguage,
    EmailProvider,
    MailType,
    ProductionAccessRequest,
    ReviewStatus,
    SendingQuota,
)
from seskit_provider_aws_ses import SESProvider, normalise_boto_error
from seskit_provider_aws_ses.errors import NO_CREDENTIALS_MESSAGE
from seskit_provider_aws_ses.provider import SES_ACCOUNT_ACTION, SES_ACCOUNT_DETAILS_ACTION

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

PRODUCTION_ACCOUNT: dict[str, Any] = {
    "ProductionAccessEnabled": True,
    "SendingEnabled": True,
    "EnforcementStatus": "HEALTHY",
    "SendQuota": {
        "Max24HourSend": 50000.0,
        "MaxSendRate": 14.0,
        "SentLast24Hours": 1200.0,
    },
}

SANDBOX_ACCOUNT: dict[str, Any] = {
    "ProductionAccessEnabled": False,
    "SendingEnabled": True,
    "EnforcementStatus": "HEALTHY",
    "SendQuota": {"Max24HourSend": 200.0, "MaxSendRate": 1.0, "SentLast24Hours": 0.0},
}


def _client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "GetAccount")


class FakeBotoClient:
    """Stands in for whatever ``session.client(...)`` would return."""

    def __init__(self, account: dict[str, Any], raises: Exception | None = None) -> None:
        self._account = account
        self._raises = raises
        #: The last PutAccountDetails body, verbatim.
        self.put_details: dict[str, Any] = {}

    def get_caller_identity(self) -> dict[str, Any]:
        return {"Account": ACCOUNT_ID, "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/test"}

    def get_account(self) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        return self._account

    def put_account_details(self, **details: Any) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        self.put_details = details
        return {}


def _provider_and_client(
    monkeypatch: pytest.MonkeyPatch,
    account: dict[str, Any] = SANDBOX_ACCOUNT,
    raises: Exception | None = None,
) -> tuple[SESProvider, FakeBotoClient]:
    provider = SESProvider(REGION, FAKE_CREDENTIALS)
    fake = FakeBotoClient(account, raises)
    monkeypatch.setattr(provider._session, "client", lambda *a, **kw: fake)
    return provider, fake


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    account: dict[str, Any] = SANDBOX_ACCOUNT,
    raises: Exception | None = None,
) -> SESProvider:
    return _provider_and_client(monkeypatch, account, raises)[0]


# --------------------------------------------------------------- protocol ---


def test_the_ses_provider_satisfies_the_interface() -> None:
    """Structural conformance. Phases 5 and 6 add methods to this Protocol; if
    the adapter drifts out of shape, this is where it shows.
    """
    assert isinstance(SESProvider(REGION, FAKE_CREDENTIALS), EmailProvider)


# ---------------------------------------------------------------- parsing ---


async def test_a_sandboxed_account_is_reported_as_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _provider(monkeypatch, SANDBOX_ACCOUNT).verify_account()

    assert status.sandbox is True
    assert status.account_id == ACCOUNT_ID
    assert status.region == REGION


async def test_a_production_account_is_not_reported_as_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _provider(monkeypatch, PRODUCTION_ACCOUNT).verify_account()

    assert status.sandbox is False
    assert status.quota == SendingQuota(
        max_24_hour_send=50000.0, max_send_rate=14.0, sent_last_24_hours=1200.0
    )


async def test_a_missing_production_flag_is_read_as_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safe reading. Claiming production access an account does not have is
    exactly the failure §8 exists to prevent.
    """
    status = await _provider(monkeypatch, {"SendingEnabled": True}).verify_account()

    assert status.sandbox is True


async def test_a_missing_quota_becomes_zeroes_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _provider(monkeypatch, {"ProductionAccessEnabled": True}).verify_account()

    assert status.quota.max_24_hour_send == 0.0


async def test_get_sending_quota_returns_the_accounts_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota = await _provider(monkeypatch, PRODUCTION_ACCOUNT).get_sending_quota()

    assert quota.max_send_rate == 14.0


# ------------------------------------------------------- production access ---


REQUEST = ProductionAccessRequest(
    mail_type=MailType.TRANSACTIONAL,
    website_url="https://example.com",
    contact_addresses=("ops@example.com", "dev@example.com"),
    contact_language=ContactLanguage.EN,
    use_case_description="Transactional mail; bounces suppressed automatically.",
)


async def test_requesting_production_access_sends_exactly_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same call `aws sesv2 put-account-details` makes, field for field.
    `ProductionAccessEnabled` is the request itself; without it the call only
    updates contact details.
    """
    provider, fake = _provider_and_client(monkeypatch)

    await provider.request_production_access(REQUEST)

    assert fake.put_details == {
        "ProductionAccessEnabled": True,
        "MailType": "TRANSACTIONAL",
        "WebsiteURL": "https://example.com",
        "ContactLanguage": "EN",
        "UseCaseDescription": "Transactional mail; bounces suppressed automatically.",
        "AdditionalContactEmailAddresses": ["ops@example.com", "dev@example.com"],
    }


async def test_an_empty_description_and_no_contacts_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SES rejects an empty list and an empty string where it wants absence."""
    provider, fake = _provider_and_client(monkeypatch)

    await provider.request_production_access(
        ProductionAccessRequest(MailType.MARKETING, "https://example.com", ())
    )

    assert "UseCaseDescription" not in fake.put_details
    assert "AdditionalContactEmailAddresses" not in fake.put_details
    assert fake.put_details["MailType"] == "MARKETING"


async def test_a_pending_review_is_read_from_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the request got to comes back on the same call as the sandbox
    flag, so the dashboard can say "asked, waiting" instead of "in the sandbox"
    with a button that would now fail.
    """
    account = {
        **SANDBOX_ACCOUNT,
        "Details": {"ReviewDetails": {"Status": "PENDING", "CaseId": "1234567890"}},
    }

    status = await _provider(monkeypatch, account).verify_account()

    assert status.sandbox is True
    assert status.review_status is ReviewStatus.PENDING
    assert status.review_case_id == "1234567890"


async def test_an_account_that_never_asked_has_no_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _provider(monkeypatch, SANDBOX_ACCOUNT).verify_account()

    assert status.review_status is None
    assert status.review_case_id is None


async def test_a_review_status_ses_invents_later_is_none_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = {**SANDBOX_ACCOUNT, "Details": {"ReviewDetails": {"Status": "ESCALATED"}}}

    status = await _provider(monkeypatch, account).verify_account()

    assert status.review_status is None


async def test_a_second_request_during_review_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SES answers ConflictException while a review is open. Named, not the
    generic "could not complete" - the user did nothing wrong and the fix is
    to wait.
    """
    provider = _provider(monkeypatch, raises=_client_error("ConflictException"))

    with pytest.raises(APIError) as caught:
        await provider.request_production_access(REQUEST)

    assert caught.value.error_type is ErrorType.INVALID_REQUEST
    assert "still reviewing" in caught.value.message


async def test_a_key_without_the_permission_is_told_which_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one new IAM action this feature needs. A key made from the older
    documented policy lacks it, and the message has to say the line to add.
    """
    provider = _provider(monkeypatch, raises=_client_error("AccessDeniedException"))

    with pytest.raises(APIError) as caught:
        await provider.request_production_access(REQUEST)

    assert caught.value.error_type is ErrorType.AUTHORIZATION_FAILED
    assert SES_ACCOUNT_DETAILS_ACTION in caught.value.message


# ----------------------------------------------------------------- errors ---


async def test_an_access_denial_names_the_missing_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Access denied" without naming the action leaves the user guessing which
    permission to add.
    """
    provider = _provider(monkeypatch, raises=_client_error("AccessDeniedException"))

    with pytest.raises(APIError) as caught:
        await provider.verify_account()

    assert caught.value.error_type is ErrorType.AUTHORIZATION_FAILED
    assert SES_ACCOUNT_ACTION in caught.value.message


async def test_a_provider_failure_never_carries_the_botocore_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§19: raw AWS exceptions must not reach a customer. A botocore string
    routinely carries the calling principal's full ARN.
    """
    secret = "arn:aws:iam::999999999999:user/internal-admin"
    provider = _provider(monkeypatch, raises=_client_error("SomethingUnmapped", secret))

    with pytest.raises(APIError) as caught:
        await provider.verify_account()

    assert caught.value.error_type is ErrorType.PROVIDER_ERROR
    assert secret not in caught.value.message


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NoCredentialsError(), ErrorType.AUTHORIZATION_FAILED),
        (_client_error("AccessDenied"), ErrorType.AUTHORIZATION_FAILED),
        (_client_error("InvalidClientTokenId"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("SignatureDoesNotMatch"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("IncompleteSignature"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("ExpiredToken"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("ExpiredToken"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("NotFoundException"), ErrorType.NOT_FOUND),
        (_client_error("ThrottlingException"), ErrorType.PROVIDER_ERROR),
        (EndpointConnectionError(endpoint_url="https://example"), ErrorType.PROVIDER_ERROR),
        (ValueError("not a botocore error at all"), ErrorType.PROVIDER_ERROR),
    ],
)
def test_each_botocore_failure_maps_to_its_error_type(exc: Exception, expected: ErrorType) -> None:
    assert normalise_boto_error(exc, action=SES_ACCOUNT_ACTION).error_type is expected


@pytest.mark.parametrize(
    ("code", "says"),
    [
        ("InvalidClientTokenId", "does not recognise that access key ID"),
        ("InvalidAccessKeyId", "does not recognise that access key ID"),
        ("SignatureDoesNotMatch", "secret access key does not match"),
        ("IncompleteSignature", "not an access key ID"),
        ("ExpiredToken", "expired"),
    ],
)
def test_a_rejected_credential_says_which_half_was_wrong(code: str, says: str) -> None:
    """One sentence for four different refusals sent a user to rotate a key
    that was fine, twice. A password manager had refilled the secret field
    under a new key ID, and "rejected - expired or incorrect" pointed at the
    key. AWS says which half is wrong; so does this.
    """
    error = normalise_boto_error(_client_error(code), action=SES_ACCOUNT_ACTION)

    assert error.error_type is ErrorType.AUTHENTICATION_FAILED
    assert says in error.message


def test_missing_credentials_name_the_credential_chain() -> None:
    """A self-hoster told only "no credentials" has nowhere to start looking."""
    error = normalise_boto_error(NoCredentialsError(), action=SES_ACCOUNT_ACTION)

    assert error.message == NO_CREDENTIALS_MESSAGE
    assert "environment variables" in error.message


def test_an_unmapped_error_is_never_re_raised_as_itself() -> None:
    """Everything becomes an APIError, including exceptions botocore never
    raised. This sits on a request path; an unexpected type escaping here
    reaches a customer as a traceback.
    """
    error = normalise_boto_error(RuntimeError("kaboom"), action=SES_ACCOUNT_ACTION)

    assert isinstance(error, APIError)
    assert "kaboom" not in error.message
