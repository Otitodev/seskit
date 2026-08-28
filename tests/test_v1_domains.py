"""``GET /v1/domains`` (§23).

The public read of a project's sending domains, authenticated by an API key.
The tenancy boundary is the point: the project comes from the key, so there is
no parameter to tamper with, and this proves the scoping rather than the absence
of a check.
"""

from __future__ import annotations

from fakes.ses import FakeProviderFactory
from httpx import AsyncClient
from redis.asyncio import Redis
from seskit_core.services import (
    add_identity,
    create_api_key,
    create_project,
    register_user,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
DOMAINS_URL = "/v1/domains"
REGION = "us-east-1"


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _project_with_key(
    session: AsyncSession,
    factory: FakeProviderFactory,
    *,
    email: str = "owner@example.com",
    domain: str = "example.com",
    address: str | None = None,
) -> str:
    """Create a project holding one domain, and return a raw API key for it."""
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")

    await add_identity(session, factory, project_id=project.id, value=domain, region=REGION)
    if address is not None:
        await add_identity(session, factory, project_id=project.id, value=address, region=REGION)

    issued = await create_api_key(session, project_id=project.id, name="prod")
    return issued.raw_key


async def test_a_key_lists_its_projects_domains(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    raw_key = await _project_with_key(db_session, provider_factory)

    response = await app_client.get(DOMAINS_URL, headers=_auth(raw_key))

    assert response.status_code == 200
    assert [item["value"] for item in response.json()["data"]] == ["example.com"]


async def test_email_identities_are_not_listed(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """§23 names this endpoint for domains. An address exists to get a human
    sending quickly; an application managing infrastructure wants domains.
    """
    raw_key = await _project_with_key(db_session, provider_factory, address="someone@example.com")

    response = await app_client.get(DOMAINS_URL, headers=_auth(raw_key))

    values = [item["value"] for item in response.json()["data"]]
    assert values == ["example.com"]
    assert "someone@example.com" not in response.text


async def test_the_dns_records_are_included(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """So a caller can render setup instructions without a second request."""
    raw_key = await _project_with_key(db_session, provider_factory)

    response = await app_client.get(DOMAINS_URL, headers=_auth(raw_key))

    records = response.json()["data"][0]["dns_records"]
    assert len(records) == 3
    assert records[0]["record_type"] == "CNAME"
    assert "_domainkey" in records[0]["name"]


async def test_internal_state_is_not_exposed(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """last_error describes our connection to AWS, not anything the caller can
    act on. Putting it in a response would make it a contract.
    """
    raw_key = await _project_with_key(db_session, provider_factory)

    response = await app_client.get(DOMAINS_URL, headers=_auth(raw_key))

    assert "last_error" not in response.text
    assert "dkim_tokens" not in response.text


async def test_a_key_sees_only_its_own_projects_domains(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """The tenancy boundary. There is no request parameter to tamper with - the
    project comes from the key.
    """
    mine = await _project_with_key(
        db_session, provider_factory, email="me@example.com", domain="mine.example"
    )
    await _project_with_key(
        db_session, provider_factory, email="them@example.com", domain="theirs.example"
    )

    response = await app_client.get(DOMAINS_URL, headers=_auth(mine))

    assert [item["value"] for item in response.json()["data"]] == ["mine.example"]
    assert "theirs.example" not in response.text


async def test_an_unauthenticated_request_gets_json_not_a_redirect(
    app_client: AsyncClient,
) -> None:
    response = await app_client.get(DOMAINS_URL, follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_failed"
    assert "<html" not in response.text.lower()


async def test_rate_limit_headers_are_present(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    raw_key = await _project_with_key(db_session, provider_factory)

    response = await app_client.get(DOMAINS_URL, headers=_auth(raw_key))

    assert int(response.headers["X-RateLimit-Limit"]) > 0
    assert int(response.headers["X-RateLimit-Remaining"]) >= 0


async def test_a_revoked_key_cannot_read_domains(
    app_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    provider_factory: FakeProviderFactory,
) -> None:
    from seskit_core.models import APIKey
    from seskit_core.security.api_keys import hash_key
    from seskit_core.services import revoke_api_key
    from sqlalchemy import select

    raw_key = await _project_with_key(db_session, provider_factory)
    assert (await app_client.get(DOMAINS_URL, headers=_auth(raw_key))).status_code == 200

    api_key = await db_session.scalar(select(APIKey).where(APIKey.hashed_key == hash_key(raw_key)))
    assert api_key is not None
    await revoke_api_key(db_session, redis_client, api_key)

    assert (await app_client.get(DOMAINS_URL, headers=_auth(raw_key))).status_code == 401


async def test_the_endpoint_is_documented(app_client: AsyncClient) -> None:
    """§23: the schema is for customers and must be good enough to generate a
    client from.
    """
    schema = (await app_client.get("/openapi.json")).json()

    assert DOMAINS_URL in schema["paths"]
    responses = schema["paths"][DOMAINS_URL]["get"]["responses"]
    assert "401" in responses
    assert "429" in responses
    # Dashboard routes stay out of it.
    assert "/domains" not in schema["paths"]
