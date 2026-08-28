"""The Domains page, over HTTP.

Split from ``test_identities.py`` so the model and service questions stay
separate from the interface ones. What is checked here is what a signed-in
person can actually do, and what they are told when it will not work.
"""

from __future__ import annotations

from fakes.ses import FakeProviderFactory
from httpx import AsyncClient
from redis.asyncio import Redis
from seskit_core.services import connect_aws
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
DOMAIN = "example.com"
ADDRESS = "someone@example.com"
REGION = "us-east-1"


async def _sign_in(client: AsyncClient, email: str = "owner@example.com") -> None:
    response = await client.post(
        "/signup", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


async def _csrf(client: AsyncClient) -> str:
    page = await client.get("/domains")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _connect(client: AsyncClient) -> str:
    """Sign in and connect AWS, which adding an identity requires."""
    await _sign_in(client)
    token = await _csrf(client)
    response = await client.post("/aws/connect", data={"csrf_token": token, "region": REGION})
    assert response.status_code == 200, response.text
    return token


# ------------------------------------------------------------------- page ---


async def test_the_page_needs_a_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/domains", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_without_an_aws_connection_the_page_says_so(app_client: AsyncClient) -> None:
    """A precondition, not a failure: an identity needs a region and
    credentials, and both come from the connection.
    """
    await _sign_in(app_client)

    page = await app_client.get("/domains")

    assert page.status_code == 200
    assert "Connect AWS first" in page.text
    assert 'href="/aws"' in page.text


async def test_a_connected_project_is_offered_the_add_form(app_client: AsyncClient) -> None:
    await _connect(app_client)

    page = await app_client.get("/domains")

    assert 'action="/domains"' in page.text
    assert "Nothing verified yet" in page.text


# ------------------------------------------------------------------ adding ---


async def test_adding_a_domain_shows_its_dns_records(app_client: AsyncClient) -> None:
    token = await _connect(app_client)

    page = await app_client.post("/domains", data={"csrf_token": token, "value": DOMAIN})

    assert page.status_code == 200
    assert "_domainkey" in page.text
    assert "dkim.amazonses.com" in page.text


async def test_adding_an_address_asks_them_to_check_their_inbox(
    app_client: AsyncClient,
) -> None:
    """The five-minute path. No DNS records should appear anywhere for it."""
    token = await _connect(app_client)

    page = await app_client.post("/domains", data={"csrf_token": token, "value": ADDRESS})

    assert "Check your inbox" in page.text
    assert "_domainkey" not in page.text


async def test_adding_without_a_connection_is_refused(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/domains", data={"csrf_token": token, "value": DOMAIN})

    assert page.status_code == 400
    assert "Connect an AWS account" in page.text


async def test_nonsense_gets_a_useful_message_not_a_traceback(
    app_client: AsyncClient,
) -> None:
    token = await _connect(app_client)

    page = await app_client.post("/domains", data={"csrf_token": token, "value": "not-a-domain"})

    assert page.status_code == 400
    assert "example.com" in page.text


# --------------------------------------------------------- refresh, delete ---


async def test_refreshing_reflects_the_new_state(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    token = await _connect(app_client)
    await app_client.post("/domains", data={"csrf_token": token, "value": DOMAIN})
    provider_factory.provider.mark_verified(DOMAIN)

    page = await app_client.get("/domains")
    assert "Pending" in page.text

    refreshed = await app_client.post(
        f"/domains/{await _first_id(app_client)}/refresh", data={"csrf_token": token}
    )

    assert "Verified" in refreshed.text


async def test_removing_an_identity_takes_it_off_the_page(app_client: AsyncClient) -> None:
    token = await _connect(app_client)
    await app_client.post("/domains", data={"csrf_token": token, "value": DOMAIN})
    identity_id = await _first_id(app_client)

    page = await app_client.post(f"/domains/{identity_id}/delete", data={"csrf_token": token})

    # Not "DOMAIN not in page.text" - the add form carries example.com as its
    # placeholder, so that assertion could never pass.
    assert f"/domains/{identity_id}/" not in page.text
    assert "Nothing verified yet" in page.text


async def test_an_unknown_identity_id_does_not_error(app_client: AsyncClient) -> None:
    """Already removed, or never theirs. Either way the page now shows the
    truth, which is more useful than an error.
    """
    token = await _connect(app_client)

    page = await app_client.post("/domains/dom_01NOPE/delete", data={"csrf_token": token})

    assert page.status_code == 200


# ------------------------------------------------------------------- csrf ---


async def test_adding_without_a_csrf_token_is_refused(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    await _connect(app_client)
    before = provider_factory.provider.create_calls

    page = await app_client.post("/domains", data={"value": DOMAIN})

    assert page.status_code == 403
    assert provider_factory.provider.create_calls == before


async def test_deleting_without_a_csrf_token_is_refused(app_client: AsyncClient) -> None:
    token = await _connect(app_client)
    await app_client.post("/domains", data={"csrf_token": token, "value": DOMAIN})
    identity_id = await _first_id(app_client)

    page = await app_client.post(f"/domains/{identity_id}/delete", data={"csrf_token": "forged"})

    assert page.status_code == 403
    assert DOMAIN in (await app_client.get("/domains")).text


# -------------------------------------------------------------- boundaries ---


async def test_a_user_cannot_delete_another_projects_identity(
    app_client: AsyncClient, db_session: AsyncSession, provider_factory: FakeProviderFactory
) -> None:
    """Ownership is part of the lookup, so an id from elsewhere resolves to
    nothing rather than to someone else's identity.
    """
    from seskit_core.services import add_identity, create_project, register_user

    # Sign in first. Registering the stranger closes signup, so doing it the
    # other way round leaves our own user unable to get an account.
    token = await _connect(app_client)

    stranger = await register_user(
        db_session, email="stranger@example.com", password=PASSWORD, allow_signup=True
    )
    other_project = await create_project(db_session, user_id=stranger.id, name="Theirs")
    await connect_aws(
        db_session,
        _redis_of(app_client),
        provider_factory,
        project_id=other_project.id,
        region=REGION,
    )
    theirs = await add_identity(
        db_session, provider_factory, project_id=other_project.id, value=DOMAIN, region=REGION
    )
    await db_session.flush()

    page = await app_client.post(f"/domains/{theirs.id}/delete", data={"csrf_token": token})

    assert page.status_code == 200
    assert provider_factory.provider.delete_calls == 0


async def _first_id(client: AsyncClient) -> str:
    """The id of the only identity on the page, read back out of the markup."""
    page = await client.get("/domains")
    marker = 'action="/domains/'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index("/", start)]


def _redis_of(client: AsyncClient) -> Redis:
    """The Redis the app under test is using, for setup that bypasses HTTP."""
    from seskit_core.redis import get_redis

    app = client._transport.app  # type: ignore[attr-defined]
    redis: Redis = app.dependency_overrides[get_redis]()
    return redis
