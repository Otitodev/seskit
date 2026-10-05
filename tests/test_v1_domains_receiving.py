"""``receiving`` on ``GET /v1/domains`` (inbound email, Phase D).

Read-only: it reports what the dashboard set up, so an application can see which
domains receive mail and what record to publish. There is no endpoint to turn
receiving on - domains themselves are managed in the dashboard - so this only ever
describes.

The MX record is the one thing in the response somebody acts on outside SESKit, and
it is withheld until receiving is on. Published earlier, it would send senders' mail
to Amazon SES with no rule to take it, and the mail would be refused.
"""

from __future__ import annotations

from httpx import AsyncClient
from seskit_core.models import Identity
from sqlalchemy.ext.asyncio import AsyncSession
from test_v1_inbound import _auth, _project_key

URL = "/v1/domains"


async def _identity(
    session: AsyncSession,
    project_id: str,
    value: str = "example.com",
    *,
    kind: str = "domain",
    region: str = "us-east-1",
    rule: str | None = None,
) -> Identity:
    identity = Identity(
        project_id=project_id,
        identity_type=kind,
        value=value,
        region=region,
        verification_status="success",
        dkim_status="success" if kind == "domain" else None,
        dkim_tokens=[],
        inbound_rule_name=rule,
        inbound_rule_set="seskit-inbound" if rule else None,
        inbound_rule_set_created=bool(rule),
    )
    session.add(identity)
    await session.flush()
    return identity


async def test_a_domain_that_does_not_receive_says_so_and_withholds_the_record(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id)
    await db_session.commit()

    (domain,) = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    assert domain["receiving"] == {"enabled": False, "mx": None}


async def test_a_domain_that_receives_gives_the_record_to_publish(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id, rule="seskit-example-com")
    await db_session.commit()

    (domain,) = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    assert domain["receiving"] == {
        "enabled": True,
        "mx": {
            "record_type": "MX",
            "name": "example.com",
            "priority": 10,
            "value": "inbound-smtp.us-east-1.amazonaws.com",
        },
    }


async def test_the_record_names_the_endpoint_for_the_domains_own_region(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Mail is received in the region the domain is verified in. A record naming any
    other would deliver nowhere, without an error.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id, "eu.example", region="eu-west-1", rule="seskit-eu")
    await db_session.commit()

    (domain,) = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    assert domain["receiving"]["mx"]["value"] == "inbound-smtp.eu-west-1.amazonaws.com"


async def test_what_the_response_adds_is_only_what_a_caller_can_act_on(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The rule name, the rule set and the bucket are internal. A customer cannot use
    them and putting them in a response would make them a contract.
    """
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id, rule="seskit-example-com")
    await db_session.commit()

    text = (await app_client.get(URL, headers=_auth(key))).text

    assert "seskit-example-com" not in text
    assert "seskit-inbound" not in text
    assert "inbound_rule" not in text
    assert "bucket" not in text


async def test_the_existing_fields_are_unchanged(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Additive: a client written before this field existed still reads the rest."""
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id)
    await db_session.commit()

    (domain,) = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    for field in ("id", "value", "region", "verification_status", "dkim_status", "dns_records"):
        assert field in domain
    assert domain["value"] == "example.com"
    assert domain["verification_status"] == "success"


async def test_another_projects_receiving_domain_is_never_listed(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    mine, key = await _project_key(db_session, owner="a@example.com")
    theirs, _ = await _project_key(db_session, owner="b@example.com")
    await _identity(db_session, mine, "mine.example")
    await _identity(db_session, theirs, "theirs.example", rule="seskit-theirs")
    await db_session.commit()

    domains = (await app_client.get(URL, headers=_auth(key))).json()["data"]

    assert [d["value"] for d in domains] == ["mine.example"]
    assert "theirs.example" not in (await app_client.get(URL, headers=_auth(key))).text


async def test_an_email_address_identity_is_still_not_a_domain(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    project_id, key = await _project_key(db_session, owner="a@example.com")
    await _identity(db_session, project_id, "me@example.com", kind="email_address")
    await db_session.commit()

    assert (await app_client.get(URL, headers=_auth(key))).json()["data"] == []
