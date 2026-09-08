"""Does a project actually send on its own key? (§31 Phase 14)

Every other test in the suite would pass if credentials were resolved once, at
process level, and handed to everybody - which is exactly what SESKit did
before this phase and exactly what it must not do now. The seam commit changed
the plumbing; nothing yet proved the water goes where it should.

So this file asks the questions that only have an answer once credentials are
per project:

- does a send use the *sending* project's key, or the other project's?
- does a project with no connection still reach SMTP, needing no key at all?
- when a stored key cannot be read, does the send fail - or quietly fall back
  to whatever the host happens to be able to reach?

That last one is the whole reason `build_session` takes credentials explicitly.
botocore would happily find an instance role or an environment variable if they
were omitted, and a project whose key was wrong would then send as somebody
else's account with nothing to say it had happened.
"""

from __future__ import annotations

from typing import Any

import pytest
from fakes.ses import ACCOUNT_ID, TEST_SECRET_KEY, FakeProviderFactory
from seskit_core.errors import APIError
from seskit_core.models import AWSConnection, ConnectionStatus, Email, EmailProvider, EmailStatus
from seskit_core.providers import AWSCredentials
from seskit_core.security.aws_credentials import encrypt_secret_access_key
from seskit_core.services import (
    add_identity,
    create_project,
    distinct_event_queues,
    register_user,
    stored_credentials,
)
from seskit_worker import sending
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
REGION = "us-east-1"
#: The application's own secret, not a literal.
#:
#: `send_one` reads it from settings rather than taking it as an argument, so a
#: test that encrypted under any other value would find the key unreadable and
#: the send recorded as failed - which looks exactly like the bug this file
#: exists to catch, and is not one.
SECRET_KEY = TEST_SECRET_KEY

#: Two projects, two IAM users, two AWS accounts. The point of the phase.
ONE = AWSCredentials(access_key_id="AKIAPROJECTONE000001", secret_access_key="secret-for-one")
TWO = AWSCredentials(access_key_id="AKIAPROJECTTWO000002", secret_access_key="secret-for-two")


class _Stub:
    """Accepts anything and remembers nothing but the fact of it."""

    async def send_email(self, message: Any) -> Any:
        from seskit_core.providers.types import SentMessage

        return SentMessage(provider_message_id="stub-message-id")


def _recording(seen: dict[str, Any]) -> Any:
    """A provider builder that records the arguments it was handed.

    The arguments are the test: which key reached the adapter is the whole
    question, and it is settled before a single byte would go anywhere.
    """

    def build(name: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _Stub()

    return build


async def _project(session: AsyncSession, *, owner: str, name: str) -> str:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name=name)
    return str(project.id)


async def _connect(
    session: AsyncSession,
    project_id: str,
    credentials: AWSCredentials,
    *,
    region: str = REGION,
    status: str = ConnectionStatus.CONNECTED.value,
    queue_url: str | None = None,
    encrypted_with: str = SECRET_KEY,
) -> AWSConnection:
    connection = AWSConnection(
        project_id=project_id,
        region=region,
        aws_account_id=ACCOUNT_ID,
        status=status,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key_encrypted=encrypt_secret_access_key(
            credentials.secret_access_key, secret_key=encrypted_with
        ),
    )
    if queue_url:
        connection.event_queue_url = queue_url
        connection.configuration_set = "seskit"
    session.add(connection)
    await session.flush()
    return connection


async def _queued(session: AsyncSession, project_id: str) -> Email:
    email = Email(
        project_id=project_id,
        from_address="hello@example.com",
        to_addresses=["user@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Hello",
        text_body="Hello",
        status=EmailStatus.QUEUED.value,
        provider=EmailProvider.SES.value,
    )
    session.add(email)
    await session.flush()
    return email


# --------------------------------------------------------------- sending ---


async def test_a_send_uses_the_sending_projects_key(db_session: AsyncSession) -> None:
    """The claim the phase makes. Two projects, two keys, and the message must
    go out on the one belonging to whoever sent it.
    """
    mine = await _project(db_session, owner="one@example.com", name="One")
    theirs = await _project(db_session, owner="two@example.com", name="Two")
    await _connect(db_session, mine, ONE)
    await _connect(db_session, theirs, TWO)
    email = await _queued(db_session, mine)

    seen: dict[str, Any] = {}

    await sending.send_one(db_session, email.id, build=_recording(seen))

    assert seen["credentials"] == ONE
    assert seen["credentials"] != TWO


async def test_a_project_with_no_connection_needs_no_key(db_session: AsyncSession) -> None:
    """The SMTP path, which is rung zero of the friction ladder: a send works
    before any AWS account exists, and must not start demanding a key.
    """
    project_id = await _project(db_session, owner="one@example.com", name="One")
    email = await _queued(db_session, project_id)
    email.provider = EmailProvider.SMTP.value

    seen: dict[str, Any] = {}

    await sending.send_one(db_session, email.id, build=_recording(seen))

    assert seen["credentials"] is None


async def test_an_unreadable_key_fails_rather_than_falling_back(
    db_session: AsyncSession,
) -> None:
    """The failure this design exists to prevent.

    A key encrypted under a different SECRET_KEY cannot be read. The send has
    to stop there. If it carried on with no credentials, botocore would find an
    instance role or an environment variable and the message would go out from
    an account nobody chose - succeeding, and wrong.
    """
    project_id = await _project(db_session, owner="one@example.com", name="One")
    await _connect(db_session, project_id, ONE, encrypted_with="a-different-secret")
    email = await _queued(db_session, project_id)

    built = False

    def build(name: str, **kwargs: Any) -> Any:
        nonlocal built
        built = True
        raise AssertionError("a provider must not be built without a usable key")

    status = await sending.send_one(db_session, email.id, build=build)

    # Failed, not retried. A key stays unreadable until somebody reconnects the
    # project, so ARQ retrying it on a schedule would be pure repetition - and
    # the message would sit in `queued` with nothing to say why.
    assert status == EmailStatus.FAILED.value
    assert built is False
    assert email.last_error is not None
    assert "SECRET_KEY" in email.last_error


# ---------------------------------------------------------------- queues ---


async def test_each_queue_comes_back_with_the_key_that_opens_it(
    db_session: AsyncSession,
) -> None:
    one = await _project(db_session, owner="one@example.com", name="One")
    await _connect(db_session, one, ONE, queue_url="https://sqs/1/seskit-events")

    queues = await distinct_event_queues(db_session, secret_key=SECRET_KEY)

    assert queues == [(REGION, "https://sqs/1/seskit-events", ONE)]


async def test_a_shared_queue_is_polled_once(db_session: AsyncSession) -> None:
    """Projects in one account and region share a queue. Polling it per project
    would have several consumers racing, each stealing events from the others'
    batches and doing the same work twice.
    """
    url = "https://sqs/1/seskit-events"
    one = await _project(db_session, owner="one@example.com", name="One")
    two = await _project(db_session, owner="two@example.com", name="Two")
    await _connect(db_session, one, ONE, queue_url=url)
    await _connect(db_session, two, ONE, queue_url=url)

    queues = await distinct_event_queues(db_session, secret_key=SECRET_KEY)

    assert len(queues) == 1


async def test_a_working_key_is_preferred_for_a_shared_queue(
    db_session: AsyncSession,
) -> None:
    """Where several projects share a queue, one of them may have had its key
    revoked. Picking that row would stop the events of everyone else on it.
    """
    url = "https://sqs/1/seskit-events"
    broken = await _project(db_session, owner="broken@example.com", name="Broken")
    working = await _project(db_session, owner="working@example.com", name="Working")
    await _connect(db_session, broken, TWO, queue_url=url, status=ConnectionStatus.ERROR.value)
    await _connect(db_session, working, ONE, queue_url=url)

    queues = await distinct_event_queues(db_session, secret_key=SECRET_KEY)

    assert [credentials for _, _, credentials in queues] == [ONE]


async def test_one_unreadable_key_does_not_stop_the_other_queues(
    db_session: AsyncSession,
) -> None:
    """A project whose SECRET_KEY changed must not take every other project's
    delivery events down with it.
    """
    one = await _project(db_session, owner="one@example.com", name="One")
    two = await _project(db_session, owner="two@example.com", name="Two")
    await _connect(
        db_session,
        one,
        ONE,
        queue_url="https://sqs/1/broken",
        encrypted_with="a-different-secret",
    )
    await _connect(db_session, two, TWO, queue_url="https://sqs/2/working")

    queues = await distinct_event_queues(db_session, secret_key=SECRET_KEY)

    assert [url for _, url, _ in queues] == ["https://sqs/2/working"]


async def test_a_project_with_no_queue_is_not_polled(db_session: AsyncSession) -> None:
    project_id = await _project(db_session, owner="one@example.com", name="One")
    await _connect(db_session, project_id, ONE)

    assert await distinct_event_queues(db_session, secret_key=SECRET_KEY) == []


# ------------------------------------------------------------ identities ---


async def test_verifying_a_sender_uses_the_projects_key(db_session: AsyncSession) -> None:
    """An identity carries a region but not a connection, so the service has to
    find one. Finding the wrong project's would verify a domain in an account
    the user does not own.
    """
    mine = await _project(db_session, owner="one@example.com", name="One")
    theirs = await _project(db_session, owner="two@example.com", name="Two")
    await _connect(db_session, mine, ONE)
    await _connect(db_session, theirs, TWO)
    factory = FakeProviderFactory()

    await add_identity(
        db_session,
        factory,
        project_id=mine,
        value="example.com",
        region=REGION,
        secret_key=SECRET_KEY,
    )

    assert factory.credentials == ONE


# ------------------------------------------------------------- decryption ---


async def test_a_stored_key_round_trips_through_the_row(db_session: AsyncSession) -> None:
    """What `connect_aws` writes is what `stored_credentials` reads. Between
    them sits the encryption, the column and the driver.
    """
    project_id = await _project(db_session, owner="one@example.com", name="One")
    connection = await _connect(db_session, project_id, ONE)

    assert stored_credentials(connection, secret_key=SECRET_KEY) == ONE


async def test_a_row_with_no_key_says_to_connect(db_session: AsyncSession) -> None:
    """Every connection made before this phase. The migration marks them
    broken; this is the message somebody gets if one is used anyway.
    """
    project_id = await _project(db_session, owner="one@example.com", name="One")
    connection = AWSConnection(
        project_id=project_id,
        region=REGION,
        aws_account_id=ACCOUNT_ID,
        status=ConnectionStatus.CONNECTED.value,
    )
    db_session.add(connection)
    await db_session.flush()

    with pytest.raises(APIError) as raised:
        stored_credentials(connection, secret_key=SECRET_KEY)

    assert "no AWS access key" in raised.value.message
