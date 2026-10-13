"""What the Domains page says about receiving mail (inbound email, Phase D).

The view model decides whether receiving can be turned on for a domain right now,
and says why in words a person can act on when it cannot. The route that turns it
on asks it again before doing anything - the disabled button is a courtesy and
this is the check, since a form can be posted by hand - so what is asserted here is
the contract both rely on. None of it needs a database.

The MX record is worth its own tests. It is the one thing a user has to do by hand,
outside SESKit, and one with the wrong host would send their mail nowhere without
an error anywhere.
"""

from __future__ import annotations

import pytest
from seskit_api.receiving import MX_PRIORITY, mx_record, receiving_view
from seskit_core.config import Settings
from seskit_core.models import AWSConnection, ConnectionStatus, Identity
from seskit_provider_aws_ses import RECEIVING_REGION_CODES

REGION = "us-east-1"


def _settings(**overrides: str) -> Settings:
    fields = {
        "SECRET_KEY": "a-real-secret",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "SMTP_HOST": None,
        "EMAILS_FROM_EMAIL": None,
    }
    return Settings(_env_file=None, **(fields | overrides))  # type: ignore[arg-type]


def _identity(
    value: str = "example.com",
    *,
    kind: str = "domain",
    verified: bool = True,
    region: str = REGION,
    rule: str | None = None,
) -> Identity:
    return Identity(
        id="dom_1",
        project_id="proj_1",
        identity_type=kind,
        value=value,
        region=region,
        verification_status="success" if verified else "pending",
        dkim_tokens=[],
        inbound_rule_name=rule,
    )


def _connection(
    *, region: str = REGION, connected: bool = True, keyed: bool = True
) -> AWSConnection:
    return AWSConnection(
        project_id="proj_1",
        aws_account_id="123456789012",
        region=region,
        status=(ConnectionStatus.CONNECTED if connected else ConnectionStatus.ERROR).value,
        aws_access_key_id="AKIAEXAMPLE" if keyed else None,
        aws_secret_access_key_encrypted="enc" if keyed else None,
    )


def _blocked(identity: Identity, connection: AWSConnection | None, **kwargs: object) -> str | None:
    settings = kwargs.pop("settings", None) or _settings()
    view = receiving_view(identity, connection, settings, **kwargs)  # type: ignore[arg-type]
    assert view is not None
    return view.blocked


# ----------------------------------------------------------- the MX record ---


def test_the_mx_record_points_at_the_ses_endpoint_for_the_domains_own_region() -> None:
    """Mail is received in the region the domain is verified in. A record naming
    any other would deliver nowhere, silently.
    """
    record = mx_record(_identity(region="eu-west-1"))

    assert record.name == "example.com"
    assert record.value == "inbound-smtp.eu-west-1.amazonaws.com"
    assert record.priority == MX_PRIORITY == 10
    assert record.zone_line == "example.com  MX  10 inbound-smtp.eu-west-1.amazonaws.com"


@pytest.mark.parametrize("region", sorted(RECEIVING_REGION_CODES))
def test_every_receiving_region_has_the_endpoint_ses_publishes(region: str) -> None:
    assert mx_record(_identity(region=region)).value == f"inbound-smtp.{region}.amazonaws.com"


# ---------------------------------------------------------- when it is on ---


def test_a_domain_that_can_receive_is_not_blocked() -> None:
    view = receiving_view(_identity(), _connection(), _settings())

    assert view is not None
    assert view.enabled is False
    assert view.blocked is None
    assert view.retention_days == 30


def test_a_domain_already_receiving_is_never_blocked() -> None:
    """A running thing is not "blocked", whatever has changed since. Reporting an
    unsupported region over a working rule would hide the control that matters,
    which is the one to stop it.
    """
    view = receiving_view(
        _identity(rule="seskit-x", region="ap-south-2"),
        _connection(connected=False),
        _settings(),
        taken_elsewhere=True,
    )

    assert view is not None
    assert view.enabled is True
    assert view.blocked is None


def test_the_retention_shown_is_the_configured_one() -> None:
    view = receiving_view(_identity(), _connection(), _settings(INBOUND_RETENTION_DAYS="45"))

    assert view is not None
    assert view.retention_days == 45


def test_an_email_address_gets_no_receiving_panel_at_all() -> None:
    """An address verifies a sender and has no MX record to point anywhere. Showing
    a control that could never work would be a puzzle.
    """
    assert (
        receiving_view(_identity("a@example.com", kind="email_address"), _connection(), _settings())
        is None
    )


# ----------------------------------------------------- why it cannot be on ---


def test_an_unverified_domain_is_told_to_verify_first() -> None:
    reason = _blocked(_identity(verified=False), _connection())

    assert reason is not None
    assert "Verify this domain first" in reason


@pytest.mark.parametrize(
    "connection",
    [None, _connection(connected=False), _connection(keyed=False)],
    ids=["no connection", "errored connection", "no stored key"],
)
def test_a_project_that_cannot_reach_aws_is_told_to_connect(
    connection: AWSConnection | None,
) -> None:
    reason = _blocked(_identity(), connection)

    assert reason is not None
    assert "Connect AWS first" in reason


def test_a_domain_in_another_region_than_the_connection_says_both() -> None:
    reason = _blocked(_identity(region="eu-west-1"), _connection(region="us-east-1"))

    assert reason is not None
    assert "eu-west-1" in reason
    assert "us-east-1" in reason


@pytest.mark.parametrize("region", ["ap-south-2", "ap-southeast-5", "eu-central-2", "ca-west-1"])
def test_a_region_that_can_send_but_not_receive_says_so_and_offers_ones_that_can(
    region: str,
) -> None:
    reason = _blocked(_identity(region=region), _connection(region=region))

    assert reason is not None
    assert f"cannot receive mail in {region}" in reason
    assert "us-east-1" in reason


def test_an_instance_that_only_takes_events_over_https_is_told_what_to_change() -> None:
    settings = _settings(EVENT_INGESTION="https", PUBLIC_BASE_URL="https://seskit.example.com")

    reason = _blocked(_identity(), _connection(), settings=settings)

    assert reason is not None
    assert "EVENT_INGESTION" in reason
    assert "sqs" in reason


def test_a_domain_receiving_for_somebody_else_says_a_domain_can_receive_in_one_place() -> None:
    reason = _blocked(_identity(), _connection(), taken_elsewhere=True)

    assert reason is not None
    assert "another project or region" in reason
    assert "MX" in reason


def test_the_first_thing_in_the_way_is_the_one_reported() -> None:
    """In the order a person would fix them: verification before connection before
    region. Reporting the last problem first would send them to fix something that
    was never the obstacle.
    """
    reason = _blocked(
        _identity(verified=False, region="ap-south-2"),
        None,
        taken_elsewhere=True,
    )

    assert reason is not None
    assert "Verify this domain first" in reason
