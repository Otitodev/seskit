"""Proving a notification came from SNS (§15).

Signed with a real RSA key and verified through the real code path - no
stubbed-out crypto. A test that patches the verification proves only that the
patch works, and this is the one place in SESKit where an unauthenticated
stranger can otherwise write to the database.

The two failures being guarded against, from docs/design/prior-art.md:

* checking ``TopicArn`` instead of the signature, when ``TopicArn`` is a field
  in the request body and topic ARNs are not secrets;
* fetching ``SigningCertURL`` or ``SubscribeURL`` before validating the host,
  which turns the endpoint into a server-side request forgery gadget.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from seskit_provider_aws_ses import sns_signature
from seskit_provider_aws_ses.sns_signature import (
    SignatureError,
    assert_aws_url,
    canonical_string,
    confirm_subscription,
    verify,
)

CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"
TOPIC = "arn:aws:sns:us-east-1:123456789012:seskit-events"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def certificate_pem(signing_key: rsa.RSAPrivateKey) -> bytes:
    """A self-signed certificate carrying the public half of the signing key.

    Self-signed is fine: nothing here validates a chain, and neither does SNS
    verification in general - the trust comes from the certificate having been
    fetched over HTTPS from an AWS host, which is what assert_aws_url enforces.
    """
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
def _clear_certificate_cache() -> None:
    sns_signature._CERTIFICATE_CACHE.clear()


def _client(certificate_pem: bytes, *, fetched: list[str] | None = None) -> httpx.AsyncClient:
    """An HTTP client that serves the certificate and records every URL asked for.

    The recording is the SSRF assertion: a URL that should have been refused
    must not appear here at all, because refusing *after* fetching is not
    refusing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if fetched is not None:
            fetched.append(str(request.url))
        return httpx.Response(200, content=certificate_pem)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sign(key: rsa.RSAPrivateKey, message: dict[str, Any], *, version: str = "1") -> dict[str, Any]:
    # SHA-1 because SNS SignatureVersion 1 uses it. Not a preference - a
    # verifier that refused it would refuse most real topics.
    digest = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303
    signed = dict(message)
    signed["SignatureVersion"] = version
    signed["SigningCertURL"] = CERT_URL
    signature = key.sign(canonical_string(signed), padding.PKCS1v15(), digest)
    signed["Signature"] = base64.b64encode(signature).decode()
    return signed


def _notification(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "Type": "Notification",
        "MessageId": "sns-message-1",
        "TopicArn": TOPIC,
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps({"eventType": "Delivery"}),
        "Timestamp": "2026-08-30T09:00:04.000Z",
    }
    message.update(overrides)
    return message


def _confirmation(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "sns-confirm-1",
        "TopicArn": TOPIC,
        "Message": "You have chosen to subscribe to the topic",
        "SubscribeURL": (
            "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc"
        ),
        "Token": "abc",
        "Timestamp": "2026-08-30T09:00:04.000Z",
    }
    message.update(overrides)
    return message


# ------------------------------------------------------------------ accept ---


async def test_a_genuine_notification_verifies(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    message = _sign(signing_key, _notification())

    async with _client(certificate_pem) as client:
        await verify(message, client=client)


async def test_signature_version_two_verifies(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Version 2 signs with SHA-256. AWS is migrating topics onto it, and an
    instance that only understands version 1 would start refusing real events.
    """
    message = _sign(signing_key, _notification(), version="2")

    async with _client(certificate_pem) as client:
        await verify(message, client=client)


async def test_a_notification_without_a_subject_verifies(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Subject is optional, and including it as an empty string would produce a
    canonical string that never verifies.
    """
    message = _notification()
    del message["Subject"]

    async with _client(certificate_pem) as client:
        await verify(_sign(signing_key, message), client=client)


async def test_a_confirmation_verifies(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Signed over a different set of fields than a notification."""
    async with _client(certificate_pem) as client:
        await verify(_sign(signing_key, _confirmation()), client=client)


# ------------------------------------------------------------------ refuse ---


async def test_a_tampered_message_is_refused(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """The whole point. Someone who knows the topic ARN can post a plausible
    body; what they cannot do is sign it.
    """
    message = _sign(signing_key, _notification())
    message["Message"] = json.dumps({"eventType": "Bounce"})

    async with _client(certificate_pem) as client:
        with pytest.raises(SignatureError):
            await verify(message, client=client)


async def test_a_forged_topic_does_not_help(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Checking TopicArn - as the project in docs/design/prior-art.md does - would
    accept this, because the ARN is a field in the body the attacker wrote.
    """
    message = _sign(signing_key, _notification())
    message["TopicArn"] = TOPIC.replace("123456789012", "999999999999")

    async with _client(certificate_pem) as client:
        with pytest.raises(SignatureError):
            await verify(message, client=client)


async def test_a_message_with_no_signature_is_refused(certificate_pem: bytes) -> None:
    async with _client(certificate_pem) as client:
        with pytest.raises(SignatureError):
            await verify(_notification(), client=client)


async def test_an_unknown_signature_version_is_refused(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Refused rather than guessed at: defaulting permissively would accept
    whatever version an attacker declares.
    """
    message = _sign(signing_key, _notification())
    message["SignatureVersion"] = "99"

    async with _client(certificate_pem) as client:
        with pytest.raises(SignatureError):
            await verify(message, client=client)


async def test_a_signature_that_is_not_base64_is_refused(certificate_pem: bytes) -> None:
    message = _notification()
    message["SignatureVersion"] = "1"
    message["SigningCertURL"] = CERT_URL
    message["Signature"] = "not base64 at all !!"

    async with _client(certificate_pem) as client:
        with pytest.raises(SignatureError):
            await verify(message, client=client)


# -------------------------------------------------------------------- SSRF ---


@pytest.mark.parametrize(
    "url",
    [
        "http://sns.us-east-1.amazonaws.com/cert.pem",  # not HTTPS
        "https://sns.us-east-1.amazonaws.com.evil.test/cert.pem",  # suffix trick
        "https://evil.test/sns.us-east-1.amazonaws.com/cert.pem",  # path trick
        "https://169.254.169.254/latest/meta-data/",  # instance metadata
        "https://localhost/cert.pem",
        "https://sns.us-east-1.amazonaws.evil/cert.pem",
        "",
    ],
)
def test_a_non_aws_url_is_refused(url: str) -> None:
    """Anchored at both ends, which is how this check is usually got wrong:
    ``sns.us-east-1.amazonaws.com.evil.test`` contains the right string.
    """
    with pytest.raises(SignatureError):
        assert_aws_url(url)


def test_a_real_sns_url_is_accepted() -> None:
    assert assert_aws_url(CERT_URL) == CERT_URL
    assert assert_aws_url("https://sns.cn-north-1.amazonaws.com.cn/x.pem")


async def test_a_bad_certificate_url_is_never_fetched(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """The requirement in full. Refusing *after* fetching is not refusing - the
    request has already been made from inside the network by then.
    """
    message = _sign(signing_key, _notification())
    message["SigningCertURL"] = "https://169.254.169.254/latest/meta-data/"
    fetched: list[str] = []

    async with _client(certificate_pem, fetched=fetched) as client:
        with pytest.raises(SignatureError):
            await verify(message, client=client)

    assert fetched == []


async def test_a_confirmation_url_off_aws_is_never_fetched(certificate_pem: bytes) -> None:
    """A forged SubscriptionConfirmation is the other half of the same hole."""
    fetched: list[str] = []

    async with _client(certificate_pem, fetched=fetched) as client:
        with pytest.raises(SignatureError):
            await confirm_subscription("https://169.254.169.254/latest/meta-data/", client=client)

    assert fetched == []


async def test_a_genuine_confirmation_url_is_fetched(certificate_pem: bytes) -> None:
    fetched: list[str] = []
    url = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc"

    async with _client(certificate_pem, fetched=fetched) as client:
        await confirm_subscription(url, client=client)

    assert fetched == [url]


# ------------------------------------------------------------------- cache ---


async def test_the_certificate_is_fetched_once(
    signing_key: rsa.RSAPrivateKey, certificate_pem: bytes
) -> None:
    """Otherwise every notification costs an outbound HTTPS round trip."""
    message = _sign(signing_key, _notification())
    fetched: list[str] = []

    async with _client(certificate_pem, fetched=fetched) as client:
        await verify(message, client=client)
        await verify(message, client=client)

    assert fetched == [CERT_URL]
