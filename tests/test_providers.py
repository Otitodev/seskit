"""The provider interface and its vocabulary (§26).

Nothing here touches AWS or the database. These are the types every provider
translates into, so the tests are about the contract itself: that the shapes
hold and that the arithmetic the dashboard depends on is right. Whether the SES
adapter satisfies the Protocol is checked beside the adapter, in
``test_ses_provider.py``.
"""

from __future__ import annotations

from seskit_core.models import ConnectionStatus
from seskit_core.models.aws_connection import AWSConnection
from seskit_core.providers import (
    AccountStatus,
    CredentialMode,
    EmailProvider,
    SendingQuota,
)

SANDBOX_QUOTA = SendingQuota(max_24_hour_send=200.0, max_send_rate=1.0, sent_last_24_hours=0.0)


# ------------------------------------------------------------------ quota ---


def test_remaining_today_is_the_unused_allowance() -> None:
    quota = SendingQuota(max_24_hour_send=200.0, max_send_rate=1.0, sent_last_24_hours=50.0)

    assert quota.remaining_today == 150.0


def test_remaining_today_never_goes_negative() -> None:
    """SES can report having sent more than the maximum after a quota is
    lowered. A negative budget rendered onto a dashboard is nonsense.
    """
    quota = SendingQuota(max_24_hour_send=200.0, max_send_rate=1.0, sent_last_24_hours=250.0)

    assert quota.remaining_today == 0.0


def test_a_quota_is_immutable() -> None:
    """Frozen so a caller cannot edit the numbers a provider reported and pass
    them on as though AWS had said so.
    """
    quota = SANDBOX_QUOTA

    try:
        quota.max_24_hour_send = 999.0  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("SendingQuota should be frozen")


# ---------------------------------------------------------------- account ---


def test_account_status_carries_the_sandbox_flag() -> None:
    """§8's requirement in its smallest form: the flag survives into the type
    the rest of the application sees.
    """
    status = AccountStatus(
        account_id="123456789012",
        region="us-east-1",
        sandbox=True,
        sending_enabled=True,
        enforcement_status="HEALTHY",
        quota=SANDBOX_QUOTA,
    )

    assert status.sandbox is True
    assert status.quota.max_24_hour_send == 200.0


def test_credential_mode_defaults_to_unknown() -> None:
    """A provider that cannot tell us where credentials came from should say so,
    not claim a source it did not resolve.
    """
    status = AccountStatus(
        account_id="123456789012",
        region="us-east-1",
        sandbox=False,
        sending_enabled=True,
        enforcement_status="HEALTHY",
        quota=SANDBOX_QUOTA,
    )

    assert status.credential_mode is CredentialMode.UNKNOWN


# --------------------------------------------------------------- protocol ---


def test_an_incomplete_provider_does_not_satisfy_the_interface() -> None:
    """Proves the check above can actually fail - a runtime_checkable Protocol
    that accepted anything would be a test that always passes.
    """

    class NotAProvider:
        pass

    assert not isinstance(NotAProvider(), EmailProvider)


# ------------------------------------------------------------------ model ---


def test_a_connection_reports_its_stored_quota() -> None:
    """The row renders through the same type as a live provider response, so
    one template serves both.
    """
    connection = AWSConnection(
        project_id="proj_01TEST",
        aws_account_id="123456789012",
        region="us-east-1",
        max_24_hour_send=200.0,
        max_send_rate=1.0,
        sent_last_24_hours=12.0,
    )

    assert connection.quota.remaining_today == 188.0


def test_is_connected_follows_status() -> None:
    connection = AWSConnection(
        project_id="proj_01TEST",
        aws_account_id="123456789012",
        region="us-east-1",
        status=ConnectionStatus.CONNECTED.value,
    )

    assert connection.is_connected is True


def test_a_repr_does_not_carry_account_details() -> None:
    """repr lands in logs and tracebacks. An AWS account id is not a secret, but
    it is an identifier worth not scattering by default.
    """
    connection = AWSConnection(
        id="aws_01TEST",
        project_id="proj_01TEST",
        aws_account_id="123456789012",
        region="us-east-1",
    )

    assert "123456789012" not in repr(connection)
