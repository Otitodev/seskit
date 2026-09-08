"""Connecting a project to AWS (§8, §9).

Runs against the real database and Redis with a fake provider, so the questions
asked here are about SESKit's behaviour - what gets persisted, who may see it,
what happens when AWS says no - rather than about boto3, which
``test_ses_provider.py`` covers.
"""

from __future__ import annotations

import pytest
from fakes.ses import ACCOUNT_ID, FAKE_CREDENTIALS, TEST_SECRET_KEY, FakeProviderFactory, denied
from httpx import AsyncClient
from redis.asyncio import Redis
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import ConnectionStatus, Project
from seskit_core.services import (
    connect_aws,
    create_project,
    disconnect_aws,
    get_connection,
    refresh_connection,
    register_user,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
OTHER_REGION = "eu-west-1"
INTERVAL = 300
DENIED_ACTION = "ses:GetAccount"
#: A phrase that appears once per rendered message. The action name itself is
#: no good for counting - the denial names it twice ("cannot call X. Add X").
DENIED_PHRASE = "is not permitted to call"
PRODUCTION_ACCESS_MARKER = "request-production-access"


async def _project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return str(project.id)


async def _sign_in(client: AsyncClient, email: str = "owner@example.com") -> None:
    response = await client.post(
        "/signup", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


async def _csrf(client: AsyncClient) -> str:
    page = await client.get("/aws")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _connect_form(token: str, *, region: str = REGION) -> dict[str, str]:
    """What the connect form actually posts.

    A helper rather than a literal in twenty places: the form gained two fields
    in Phase 14, and a test that posted only a region would now be testing the
    validation error rather than the thing it was written for.
    """
    return {
        "csrf_token": token,
        "region": region,
        "access_key_id": FAKE_CREDENTIALS.access_key_id,
        "secret_access_key": FAKE_CREDENTIALS.secret_access_key,
    }


# ---------------------------------------------------------------- service ---


async def test_connecting_records_what_aws_reported(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _project(db_session)
    factory = FakeProviderFactory(sandbox=True)

    connection = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    assert connection.aws_account_id == ACCOUNT_ID
    assert connection.region == REGION
    assert connection.sandbox is True
    assert connection.status == ConnectionStatus.CONNECTED.value
    assert connection.max_24_hour_send == 200.0
    assert connection.last_checked_at is not None


async def test_a_production_account_is_not_marked_sandboxed(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _project(db_session)

    connection = await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(sandbox=False),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    assert connection.sandbox is False
    assert connection.max_24_hour_send == 50000.0


async def test_connecting_twice_updates_rather_than_duplicates(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """project_id is unique. Two rows would disagree about which region the
    project sends from, and nothing would say which one wins.
    """
    project_id = await _project(db_session)
    factory = FakeProviderFactory()

    first = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )
    second = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=OTHER_REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    assert first.id == second.id
    assert second.region == OTHER_REGION


async def test_a_failed_connect_creates_no_row(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Nothing was connected, so there is nothing to describe."""
    project_id = await _project(db_session)

    with pytest.raises(APIError):
        await connect_aws(
            db_session,
            redis_client,
            FakeProviderFactory(error=denied()),
            project_id=project_id,
            region=REGION,
            credentials=FAKE_CREDENTIALS,
            secret_key=TEST_SECRET_KEY,
        )

    assert await get_connection(db_session, project_id) is None


async def test_a_working_connection_that_breaks_is_marked_broken(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """An established connection that stops working should look broken, not
    stale - the numbers on the page would otherwise still read as current.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    with pytest.raises(APIError):
        await connect_aws(
            db_session,
            redis_client,
            FakeProviderFactory(error=denied()),
            project_id=project_id,
            region=REGION,
            credentials=FAKE_CREDENTIALS,
            secret_key=TEST_SECRET_KEY,
        )

    connection = await get_connection(db_session, project_id)
    assert connection is not None
    assert connection.status == ConnectionStatus.ERROR.value
    assert DENIED_ACTION in (connection.last_error or "")


async def test_the_stored_error_is_the_normalised_one(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """§19. This string is rendered into a page, and a botocore message carries
    the calling principal's ARN.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    with pytest.raises(APIError):
        await connect_aws(
            db_session,
            redis_client,
            FakeProviderFactory(error=APIError(ErrorType.PROVIDER_ERROR, "Generic failure.")),
            project_id=project_id,
            region=REGION,
            credentials=FAKE_CREDENTIALS,
            secret_key=TEST_SECRET_KEY,
        )

    connection = await get_connection(db_session, project_id)
    assert connection is not None
    assert connection.last_error == "Generic failure."


# ---------------------------------------------------------------- refresh ---


async def test_refresh_asks_aws_again(db_session: AsyncSession, redis_client: Redis) -> None:
    project_id = await _project(db_session)
    factory = FakeProviderFactory(sandbox=True)
    connection = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    factory.provider.sandbox = False
    calls_before = factory.provider.calls
    await refresh_connection(
        db_session,
        redis_client,
        factory,
        connection,
        interval_seconds=INTERVAL,
        secret_key=TEST_SECRET_KEY,
    )

    assert factory.provider.calls == calls_before + 1
    assert connection.sandbox is False


async def test_a_second_refresh_inside_the_interval_does_not_call_aws(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Holding down Refresh must not send a request to AWS each time - AWS
    answers that by throttling the account.
    """
    project_id = await _project(db_session)
    factory = FakeProviderFactory()
    connection = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    await refresh_connection(
        db_session,
        redis_client,
        factory,
        connection,
        interval_seconds=INTERVAL,
        secret_key=TEST_SECRET_KEY,
    )
    calls_after_first = factory.provider.calls
    await refresh_connection(
        db_session,
        redis_client,
        factory,
        connection,
        interval_seconds=INTERVAL,
        secret_key=TEST_SECRET_KEY,
    )

    assert factory.provider.calls == calls_after_first


async def test_connecting_clears_the_interval_guard(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Connect is a deliberate act by someone watching the page. Making them
    wait out a marker left by a previous state would be nonsense.
    """
    project_id = await _project(db_session)
    factory = FakeProviderFactory()
    connection = await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )
    await refresh_connection(
        db_session,
        redis_client,
        factory,
        connection,
        interval_seconds=INTERVAL,
        secret_key=TEST_SECRET_KEY,
    )

    await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )
    calls_before = factory.provider.calls
    await refresh_connection(
        db_session,
        redis_client,
        factory,
        connection,
        interval_seconds=INTERVAL,
        secret_key=TEST_SECRET_KEY,
    )

    assert factory.provider.calls == calls_before + 1


# ------------------------------------------------------------- disconnect ---


async def test_disconnecting_removes_the_connection(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    project_id = await _project(db_session)
    connection = await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    await disconnect_aws(db_session, redis_client, connection, secret_key=TEST_SECRET_KEY)

    assert await get_connection(db_session, project_id) is None


# ------------------------------------------------------------- boundaries ---


async def test_projects_do_not_share_a_connection(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Two projects on one instance share an AWS account but hold their own
    region and their own status against it (§6 vs §9).
    """
    mine = await _project(db_session, email="me@example.com")
    theirs = await _project(db_session, email="them@example.com")
    factory = FakeProviderFactory()

    await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=mine,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )
    await connect_aws(
        db_session,
        redis_client,
        factory,
        project_id=theirs,
        region=OTHER_REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    mine_connection = await get_connection(db_session, mine)
    theirs_connection = await get_connection(db_session, theirs)
    assert mine_connection is not None
    assert theirs_connection is not None
    assert mine_connection.region == REGION
    assert theirs_connection.region == OTHER_REGION


async def test_deleting_a_project_deletes_its_connection(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Cascading in the database, not only the ORM: a delete issued from psql
    must not leave a connection pointing at a project that no longer exists.
    """
    project_id = await _project(db_session)
    await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    project = await db_session.get(Project, project_id)
    assert project is not None
    await db_session.delete(project)
    await db_session.flush()

    assert await get_connection(db_session, project_id) is None


# ------------------------------------------------------------------- page ---


async def test_the_page_needs_a_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/aws", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_an_unconnected_project_is_offered_the_connect_form(
    app_client: AsyncClient,
) -> None:
    await _sign_in(app_client)

    page = await app_client.get("/aws")

    assert page.status_code == 200
    assert "No AWS account connected" in page.text
    assert 'action="/aws/connect"' in page.text


async def test_connecting_through_the_page_shows_the_account(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert page.status_code == 200
    assert ACCOUNT_ID in page.text
    assert REGION in page.text


async def test_a_sandboxed_account_is_warned_about_persistently(app_client: AsyncClient) -> None:
    """§8: surfaced on the page, and still there on the next page load - not a
    one-off message shown at connect time.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))

    page = await app_client.get("/aws")

    assert "sandbox" in page.text.lower()
    assert PRODUCTION_ACCESS_MARKER in page.text


async def test_a_production_account_is_not_warned(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    provider_factory.provider.sandbox = False
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert PRODUCTION_ACCESS_MARKER not in page.text
    assert "Production access" in page.text


async def test_an_unknown_region_is_refused_without_calling_aws(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """Catching the typo here saves a round trip that would fail with a message
    about endpoints rather than about the typo.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token, region="mars-central-1"))

    assert page.status_code == 400
    assert provider_factory.builds == 0


async def test_a_denied_connect_shows_the_missing_iam_action(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """An access denial without the action named leaves the user guessing which
    permission to add.
    """
    provider_factory.provider.error = denied()
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert DENIED_ACTION in page.text


async def test_a_credential_failure_is_not_a_401(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """The user's AWS configuration is wrong, not their SESKit session. A 401
    here would read - to a browser and to this dashboard's own conventions - as
    "you are not signed in".
    """
    provider_factory.provider.error = denied()
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert page.status_code == 400


async def test_disconnecting_through_the_page_forgets_the_account(app_client: AsyncClient) -> None:
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))

    page = await app_client.post("/aws/disconnect", data={"csrf_token": token})

    assert "No AWS account connected" in page.text


async def test_refreshing_without_a_connection_does_not_error(
    app_client: AsyncClient,
) -> None:
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/refresh", data={"csrf_token": token})

    assert page.status_code == 200


# ------------------------------------------------------------------- csrf ---


async def test_connect_without_a_csrf_token_is_refused(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    await _sign_in(app_client)

    page = await app_client.post("/aws/connect", data={"region": REGION})

    assert page.status_code == 403
    assert provider_factory.builds == 0


async def test_disconnect_without_a_csrf_token_is_refused(app_client: AsyncClient) -> None:
    """State-changing and therefore a POST behind CSRF - otherwise any page on
    the internet could disconnect a user's AWS account.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))

    page = await app_client.post("/aws/disconnect", data={"csrf_token": "forged"})

    assert page.status_code == 403
    assert "No AWS account connected" not in (await app_client.get("/aws")).text


async def test_a_failed_action_shows_its_error_only_once(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """Found in the browser, not by a test.

    A failed refresh rendered the same sentence twice - once as the action's
    error and once as the connection's stored last_error - which reads as two
    different problems rather than one.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))
    provider_factory.provider.error = denied()

    page = await app_client.post("/aws/refresh", data={"csrf_token": token})

    assert page.text.count(DENIED_PHRASE) == 1


async def test_the_stored_error_survives_a_reload(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """The other half of showing it once: suppressing the duplicate must not
    lose the message on the next page load, when it is the only record of what
    went wrong.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))
    provider_factory.provider.error = denied()
    await app_client.post("/aws/refresh", data={"csrf_token": token})

    page = await app_client.get("/aws")

    assert "The last check failed" in page.text
    assert DENIED_ACTION in page.text


async def test_a_broken_connection_is_not_described_as_absent(
    app_client: AsyncClient, provider_factory: FakeProviderFactory
) -> None:
    """Also found in the browser. The page claimed "No AWS account connected"
    directly above "The last check failed" - which contradicts itself, and
    suggests the connection was lost rather than that it stopped working.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))
    provider_factory.provider.error = denied()
    await app_client.post("/aws/refresh", data={"csrf_token": token})

    page = await app_client.get("/aws")

    assert "No AWS account connected" not in page.text
    assert "Connection is not working" in page.text


async def test_a_project_that_never_connected_says_so(app_client: AsyncClient) -> None:
    """The opposite case still reads correctly - "not working" would be wrong
    for a project that has never had a connection at all.
    """
    await _sign_in(app_client)

    page = await app_client.get("/aws")

    assert "No AWS account connected" in page.text
    assert "Connection is not working" not in page.text


# ---------------------------------------------------------------- secrets ---


async def test_no_credential_material_is_ever_stored(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """§9's rule, asserted against the row rather than trusted to review: there
    is no column that could hold a secret key.
    """
    project_id = await _project(db_session)
    connection = await connect_aws(
        db_session,
        redis_client,
        FakeProviderFactory(),
        project_id=project_id,
        region=REGION,
        credentials=FAKE_CREDENTIALS,
        secret_key=TEST_SECRET_KEY,
    )

    columns = {column.name for column in connection.__table__.columns}
    forbidden = {"access_key", "secret_key", "secret_access_key", "credentials", "session_token"}

    assert not columns & forbidden


# ------------------------------------------------------------- access key ---


async def test_the_stored_key_is_shown_shortened(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Which of my keys is this? - the only question the page is asked about
    it. An access key id is an identifier rather than a secret, so this is
    shortening for a screen, not redaction.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert "AKIA\u2026MPLE" in page.text
    assert FAKE_CREDENTIALS.access_key_id not in page.text


async def test_the_secret_never_reaches_the_page(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The claim the whole design rests on. There is no route that can return
    it, and this is what would notice if one appeared.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert FAKE_CREDENTIALS.secret_access_key not in page.text
    assert "wJalr" not in page.text


async def test_the_secret_is_not_echoed_back_after_a_failure(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A form that repopulates itself would put the secret in the HTML of an
    error page - which is the page most likely to be screenshotted and pasted
    into a support thread.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token, region="mars-central-1"))

    assert page.status_code == 400
    assert FAKE_CREDENTIALS.secret_access_key not in page.text


async def test_a_connected_project_can_replace_its_key(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Rotation is routine. Without this the only way to change a key would be
    Disconnect, which tears down the delivery-event infrastructure to fix
    something unrelated to it.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)
    await app_client.post("/aws/connect", data=_connect_form(token))

    page = await app_client.post(
        "/aws/connect",
        data={
            "csrf_token": token,
            "region": REGION,
            "access_key_id": "AKIAROTATEDKEY123456",
            "secret_access_key": "a-freshly-rotated-secret",
        },
    )

    assert page.status_code == 200
    project_id = await db_session.scalar(select(Project.id))
    connection = await get_connection(db_session, str(project_id))
    assert connection is not None
    assert connection.aws_access_key_id == "AKIAROTATEDKEY123456"


async def test_replacing_the_key_is_offered_while_connected(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post("/aws/connect", data=_connect_form(token))

    assert "Replace the access key" in page.text


async def test_both_key_fields_are_required(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A blank secret must not be stored as a blank secret and discovered at
    the first send.
    """
    await _sign_in(app_client)
    token = await _csrf(app_client)

    page = await app_client.post(
        "/aws/connect",
        data={"csrf_token": token, "region": REGION, "access_key_id": "AKIA123"},
    )

    assert page.status_code == 400
    assert "required" in page.text
