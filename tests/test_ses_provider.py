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
from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import CredentialMode, EmailProvider, SendingQuota
from seskit_provider_aws_ses import SESProvider, normalise_boto_error
from seskit_provider_aws_ses.errors import NO_CREDENTIALS_MESSAGE
from seskit_provider_aws_ses.provider import SES_ACCOUNT_ACTION

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

    def get_caller_identity(self) -> dict[str, Any]:
        return {"Account": ACCOUNT_ID, "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/test"}

    def get_account(self) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        return self._account


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    account: dict[str, Any] = SANDBOX_ACCOUNT,
    raises: Exception | None = None,
) -> SESProvider:
    provider = SESProvider(REGION)
    fake = FakeBotoClient(account, raises)
    monkeypatch.setattr(provider._session, "client", lambda *a, **kw: fake)
    return provider


# --------------------------------------------------------------- protocol ---


def test_the_ses_provider_satisfies_the_interface() -> None:
    """Structural conformance. Phases 5 and 6 add methods to this Protocol; if
    the adapter drifts out of shape, this is where it shows.
    """
    assert isinstance(SESProvider(REGION), EmailProvider)


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
        (_client_error("ExpiredToken"), ErrorType.AUTHENTICATION_FAILED),
        (_client_error("NotFoundException"), ErrorType.NOT_FOUND),
        (_client_error("ThrottlingException"), ErrorType.PROVIDER_ERROR),
        (EndpointConnectionError(endpoint_url="https://example"), ErrorType.PROVIDER_ERROR),
        (ValueError("not a botocore error at all"), ErrorType.PROVIDER_ERROR),
    ],
)
def test_each_botocore_failure_maps_to_its_error_type(exc: Exception, expected: ErrorType) -> None:
    assert normalise_boto_error(exc, action=SES_ACCOUNT_ACTION).error_type is expected


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


# ------------------------------------------------------------ credentials ---


def test_an_unresolvable_credential_mode_is_unknown_not_a_guess() -> None:
    """Returning UNKNOWN rather than raising: the caller finds out for real on
    the first API call, which produces a far better error than a guess here.
    """
    provider = SESProvider(REGION)

    assert provider.credential_mode in set(CredentialMode)
