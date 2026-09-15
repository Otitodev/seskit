"""``POST /v1/events/ses`` (§15).

The status code *is* the protocol between SNS and this endpoint, so that is
what these assert. SNS retries anything that is not 2xx, which is the behaviour
wanted for a transient failure and exactly the behaviour not wanted for a
message that will never be processable.

Signatures are real here too - the certificate is seeded into the verifier's
cache so nothing reaches the network, but the RSA verification itself runs.
This endpoint is unauthenticated by design (SNS has no credential to present),
so a test that stubbed out the verification would be asserting nothing.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fakes import ses_events
from fakes.queue import FakeQueue
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from seskit_api.main import create_app
from seskit_api.queue import get_queue
from seskit_core.config import EventIngestion, Settings
from seskit_core.db import get_session
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
)
from seskit_core.redis import get_redis
from seskit_core.services import create_project, register_user
from seskit_provider_aws_ses import sns_signature
from seskit_provider_aws_ses.sns_signature import canonical_string
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
ENDPOINT = "/v1/events/ses"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"
TOPIC = "arn:aws:sns:us-east-1:123456789012:seskit-events"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def certificate_pem(signing_key: rsa.RSAPrivateKey) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(signing_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(autouse=True)
def _seed_certificate(certificate_pem: bytes) -> None:
    """Put the certificate where the verifier would have fetched it.

    Seeding the cache rather than stubbing the verification: the signature
    check still runs for real, and no test touches the network. Only URLs that
    already passed the AWS host check ever reach this cache, so seeding it
    cannot mask an SSRF failure.
    """
    sns_signature._CERTIFICATE_CACHE.clear()
    sns_signature._CERTIFICATE_CACHE[CERT_URL] = certificate_pem


def _sign(key: rsa.RSAPrivateKey, message: dict[str, Any]) -> dict[str, Any]:
    signed = dict(message)
    signed["SignatureVersion"] = "1"
    signed["SigningCertURL"] = CERT_URL
    # SHA-1 is not a choice: SNS SignatureVersion 1 signs with it, so verifying
    # a real notification means using it. Version 2 (SHA-256) is covered in
    # test_sns_signature.py.
    signature = key.sign(canonical_string(signed), padding.PKCS1v15(), hashes.SHA1())  # noqa: S303
    signed["Signature"] = base64.b64encode(signature).decode()
    return signed


def _notification(payload: dict[str, Any], *, message_id: str = "sns-1") -> dict[str, Any]:
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": TOPIC,
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps(payload),
        "Timestamp": "2026-08-30T09:00:04.000Z",
    }


@pytest.fixture
async def receiver(
    settings: Settings, db_session: AsyncSession, redis_client: Redis, queue: FakeQueue
) -> AsyncClient:
    """A client for an instance configured to accept HTTPS notifications."""
    configured = settings.model_copy(update={"EVENT_INGESTION": EventIngestion.BOTH})
    application = create_app(configured)

    async def _session() -> Any:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_redis] = lambda: redis_client
    # The receiver enqueues webhook deliveries after ingest; the ASGI transport
    # does not run lifespan, so app.state.queue is never built.
    application.dependency_overrides[get_queue] = lambda: queue

    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


async def _connected_project(
    session: AsyncSession,
    *,
    owner: str = "owner@example.com",
    account_id: str = "123456789012",
    region: str = "us-east-1",
    topic_arn: str | None = TOPIC,
) -> str:
    """A project connected to an AWS account, which is what makes a topic in
    that account one this instance will listen to.

    `topic_arn=None` models the moment SNS's confirmation arrives: the
    subscribe call has fired it, and provisioning has not yet stored the ARN.
    """
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    session.add(
        AWSConnection(
            project_id=project.id,
            region=region,
            aws_account_id=account_id,
            status=ConnectionStatus.CONNECTED.value,
            aws_access_key_id="AKIAEXAMPLE",
            event_topic_arn=topic_arn,
        )
    )
    await session.flush()
    return str(project.id)


async def _sent_email(session: AsyncSession, *, project_id: str | None = None) -> Email:
    """A sent message. The project is connected to the account the test
    topic lives in, since nothing here would be accepted otherwise.
    """
    if project_id is None:
        project_id = await _connected_project(session)
    email = Email(
        project_id=project_id,
        from_address=ses_events.SENDER,
        to_addresses=[ses_events.RECIPIENT],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hello",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id=ses_events.MESSAGE_ID,
    )
    session.add(email)
    await session.flush()
    return email


async def _count(session: AsyncSession, event_type: EventType | None = None) -> int:
    """How many events are recorded, optionally of one type.

    The filter earns its place since suppression: a permanent bounce records
    the bounce *and* the `email.suppressed` it causes, so a bare total stopped
    answering "was this notification recorded once?" - which is what every
    caller here is actually asking.
    """
    query = select(func.count()).select_from(EmailEvent)
    if event_type is not None:
        query = query.where(EmailEvent.event_type == event_type.value)
    return int(await session.scalar(query) or 0)


# ---------------------------------------------------------------- accepted ---


async def test_a_signed_delivery_is_recorded(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    await _sent_email(db_session)
    body = _sign(signing_key, _notification(ses_events.delivery()))

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 204
    assert await _count(db_session) == 1


async def test_the_same_notification_twice_records_one_event(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """SNS is at-least-once over HTTPS too, and it retries on any non-2xx -
    so a redelivery is routine, not exceptional.
    """
    await _sent_email(db_session)
    body = _sign(signing_key, _notification(ses_events.bounce(), message_id="sns-same"))

    first = await receiver.post(ENDPOINT, json=body)
    second = await receiver.post(ENDPOINT, json=body)

    assert (first.status_code, second.status_code) == (204, 204)
    assert await _count(db_session, EventType.BOUNCED) == 1


async def test_an_event_for_an_unknown_message_is_settled(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """2xx on purpose. There is no Email to attach it to and there never will
    be, so asking SNS to keep trying helps nobody.

    From our own topic - the account is connected - or the receiver would
    refuse it at the door with a 403 and this would be testing something else.
    """
    await _connected_project(db_session)
    body = _sign(signing_key, _notification(ses_events.delivery()))

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 204
    assert await _count(db_session) == 0


# ---------------------------------------------------------------- rejected ---


async def test_an_unsigned_notification_is_refused(
    receiver: AsyncClient, db_session: AsyncSession
) -> None:
    """The forgery this endpoint exists to refuse. The body is entirely
    plausible - correct topic, correct shape - and simply not signed.
    """
    response = await receiver.post(ENDPOINT, json=_notification(ses_events.bounce()))

    assert response.status_code == 403
    assert await _count(db_session) == 0


async def test_a_tampered_notification_is_refused(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Signed, then edited. A complaint rate is the number AWS suspends
    accounts over, so inventing one has to be impossible rather than awkward.
    """
    await _sent_email(db_session)
    body = _sign(signing_key, _notification(ses_events.delivery()))
    body["Message"] = json.dumps(ses_events.complaint())

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403
    assert await _count(db_session) == 0


async def test_a_certificate_url_off_aws_is_refused(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """The SSRF requirement, through the endpoint.

    Nothing is fetched: the URL never passes the host check, and no HTTP client
    is configured in this test, so a request that was attempted would fail
    loudly rather than pass.
    """
    body = _sign(signing_key, _notification(ses_events.delivery()))
    body["SigningCertURL"] = "https://169.254.169.254/latest/meta-data/"

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403


async def test_a_body_that_is_not_json_is_refused(receiver: AsyncClient) -> None:
    """4xx rather than 5xx: it will not become JSON on the next attempt."""
    response = await receiver.post(
        ENDPOINT, content=b"not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400


async def test_a_body_without_an_sns_type_is_refused(receiver: AsyncClient) -> None:
    response = await receiver.post(ENDPOINT, json={"hello": "world"})

    assert response.status_code == 400


async def test_an_oversized_body_is_refused(receiver: AsyncClient) -> None:
    """An unauthenticated endpoint must not be a way to make the process read
    an arbitrary amount into memory.
    """
    response = await receiver.post(
        ENDPOINT,
        content=b"x" * (256 * 1024 + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


# ------------------------------------------------------------- handshake ---


async def test_a_confirmation_pointing_off_aws_is_refused(
    receiver: AsyncClient, signing_key: rsa.RSAPrivateKey
) -> None:
    """A correctly signed message is still not a licence to fetch whatever URL
    it names. This one is signed by our own key and names the metadata service.
    """
    body = _sign(
        signing_key,
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-confirm",
            "TopicArn": TOPIC,
            "Message": "You have chosen to subscribe",
            "SubscribeURL": "https://169.254.169.254/latest/meta-data/",
            "Token": "abc",
            "Timestamp": "2026-08-30T09:00:04.000Z",
        },
    )

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403


# ------------------------------------------------------------ whose topic ---

# The signature proves Amazon SNS emitted a message. It does not prove whose
# topic it was, and it cannot: SNS signing keys are per-region and shared by
# every customer, so anyone with an AWS account can have SNS sign whatever they
# publish to a topic of their own. Before these tests, that was enough. A
# correctly signed notification from a stranger's topic naming a message id
# read out of a delivered email's headers attached to that message, suppressed
# whatever addresses it listed, and had SESKit sign webhooks for it.

STRANGERS_TOPIC = "arn:aws:sns:us-east-1:999999999999:their-topic"


async def test_a_signed_notification_from_a_strangers_topic_is_refused(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """The finding. Genuinely signed, names a real message, and the account in
    the topic ARN is not one anybody here connected.

    Distinct from `test_a_tampered_notification_is_refused`: that message was
    altered after signing and fails the signature. This one passes it, which
    is exactly why the signature alone was never enough.
    """
    email = await _sent_email(db_session)
    body = _sign(signing_key, {**_notification(ses_events.bounce()), "TopicArn": STRANGERS_TOPIC})

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403
    assert await _count(db_session) == 0
    assert email.delivered_at is None


async def test_a_strangers_subscription_is_never_confirmed(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The easiest version of the attack. `sns:Subscribe` on the attacker's
    own topic, endpoint set to this URL, and SNS sends a genuine signed
    confirmation. Answering it opens a push channel from their account into
    this instance. The refusal has to come before the confirmation branch.
    """
    from seskit_api.routes.v1 import events as receiver_module

    await _connected_project(db_session)
    fetched: list[str] = []

    async def record(url: str) -> None:
        fetched.append(url)

    monkeypatch.setattr(receiver_module, "confirm_subscription", record)
    body = _sign(
        signing_key,
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-confirm",
            "TopicArn": STRANGERS_TOPIC,
            "Message": "You have chosen to subscribe",
            "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
            "Token": "abc",
            "Timestamp": "2026-08-30T09:00:04.000Z",
        },
    )

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403
    assert fetched == []


async def test_our_own_confirmation_is_answered_before_the_arn_is_stored(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the binding has to survive.

    Provisioning subscribes the HTTPS endpoint before it returns, so SNS's
    confirmation arrives while `event_topic_arn` is still NULL. A check on the
    stored ARN would refuse it and break HTTPS setup outright. The account id
    is stored at connect time and is what the check reads, so this is
    answered - and the subscription confirmed - with no ARN in the row.
    """
    from seskit_api.routes.v1 import events as receiver_module

    await _connected_project(db_session, topic_arn=None)
    fetched: list[str] = []

    async def record(url: str) -> None:
        fetched.append(url)

    monkeypatch.setattr(receiver_module, "confirm_subscription", record)
    body = _sign(
        signing_key,
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-confirm",
            "TopicArn": TOPIC,
            "Message": "You have chosen to subscribe",
            "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
            "Token": "abc",
            "Timestamp": "2026-08-30T09:00:04.000Z",
        },
    )

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 204
    assert len(fetched) == 1


async def test_the_same_account_in_another_region_is_not_ours(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Topics are regional. A connection in us-east-1 does not vouch for a
    topic in eu-west-2, even in the same account.
    """
    await _sent_email(db_session)
    elsewhere = "arn:aws:sns:eu-west-2:123456789012:seskit-events"
    body = _sign(signing_key, {**_notification(ses_events.delivery()), "TopicArn": elsewhere})

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403
    assert await _count(db_session) == 0


async def test_a_topic_arn_that_is_not_one_is_refused(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    await _sent_email(db_session)
    body = _sign(signing_key, {**_notification(ses_events.delivery()), "TopicArn": "nonsense"})

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 403


async def test_an_event_names_a_message_from_a_project_on_another_account(
    receiver: AsyncClient,
    db_session: AsyncSession,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Two projects on one instance, two AWS accounts. A genuine event from
    account A's topic naming a message account B's project sent is not
    refused at the door - account A is connected - but it must not find B's
    message. Settled as unknown, and nothing recorded.
    """
    b_project = await _connected_project(
        db_session, owner="b@example.com", account_id="222222222222"
    )
    email = await _sent_email(db_session, project_id=b_project)
    await _connected_project(db_session, owner="a@example.com", account_id="123456789012")

    body = _sign(signing_key, _notification(ses_events.delivery()))  # topic in A's account

    response = await receiver.post(ENDPOINT, json=body)

    assert response.status_code == 204
    assert await _count(db_session) == 0
    assert email.delivered_at is None


# --------------------------------------------------------------- disabled ---


async def test_the_endpoint_is_absent_unless_configured(
    settings: Settings, db_session: AsyncSession, redis_client: Redis, queue: FakeQueue
) -> None:
    """SQS is the default, and an endpoint nothing is subscribed to is only an
    attack surface.
    """
    application = create_app(settings)

    async def _session() -> Any:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_redis] = lambda: redis_client
    application.dependency_overrides[get_queue] = lambda: queue

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(ENDPOINT, json={"Type": "Notification"})

    assert response.status_code == 404
