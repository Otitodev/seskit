"""What moto can genuinely mock of the AWS calls this phase makes.

§31 asked for a spike before writing tests against moto, and the spike found a
gap worth recording rather than working around.

**moto 5.2.3, checked 2026-08-27:**

===========================  =================================================
STS ``GetCallerIdentity``    implemented
SESv2 ``GetAccount``         **not implemented** - raises NotImplementedError
SES v1 ``GetSendQuota``      implemented (but v1 has no sandbox flag at all)
SESv2 ``CreateEmailIdentity``implemented, with DkimAttributes (Phase 5)
===========================  =================================================

``GetAccount`` is the one call carrying both the sandbox flag and the quota.
Falling back to SES v1 would buy a mockable quota at the cost of a second client
and would still leave sandbox detection unmocked, so the adapter keeps
``GetAccount`` and its behaviour is covered by the fake client in
``test_ses_provider.py`` instead.

That leaves moto testing what it can test honestly: that the adapter builds a
real client, resolves credentials, and reads a real STS response.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("moto", reason="moto is a dev dependency")

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
from seskit_provider_aws_ses import SESProvider

REGION = "us-east-1"

#: moto's canned account. Asserting on it is asserting on moto, which is the
#: point here - the value is that a real boto3 client reached a real response.
MOTO_ACCOUNT_ID = "123456789012"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials, so a stray real profile on the machine running the
    tests cannot be picked up and used against a real account.
    """
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


async def test_the_adapter_reads_a_real_sts_response(aws_credentials: None) -> None:
    """End to end through boto3: session, client, thread hand-off, response.

    ``_account_id`` is exercised rather than ``verify_account`` because the
    latter goes on to call GetAccount, which moto does not implement.
    """
    with mock_aws():
        provider = SESProvider(REGION)

        assert await provider._account_id() == MOTO_ACCOUNT_ID


def test_credentials_resolve_from_the_environment(aws_credentials: None) -> None:
    """Proves the credential chain is boto3's, not something reimplemented
    here - the env vars above are picked up without being passed anywhere.
    """
    from seskit_core.providers import CredentialMode

    with mock_aws():
        assert SESProvider(REGION).credential_mode is CredentialMode.ENVIRONMENT


def test_moto_still_does_not_implement_get_account(aws_credentials: None) -> None:
    """A canary, not a specification.

    If moto gains SESv2 GetAccount, this test fails - and that failure is the
    signal to move sandbox and quota coverage onto moto and delete the fake for
    those paths. Without it the gap would quietly outlive its own justification.
    """
    with mock_aws():
        client = boto3.client("sesv2", region_name=REGION)

        with pytest.raises((NotImplementedError, ClientError)):
            client.get_account()


def test_the_environment_is_not_carrying_real_credentials() -> None:
    """Guards the guard. If a developer exports a real key into the shell that
    runs the suite, the fixture above masks it - but a test that made a live
    call before the fixture applied would not be caught, so this states the
    expectation plainly.
    """
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")

    assert not key.startswith("AKIA") or key == "testing", (
        "A real-looking AWS key is set in this environment. "
        "The suite must never reach a real AWS account."
    )
