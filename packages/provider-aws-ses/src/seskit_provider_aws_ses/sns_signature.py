"""Proving an SNS notification really came from SNS (§15).

The HTTPS receiver is reachable by anyone who learns its URL, and SNS cannot
present a credential - so the signature is the only thing standing between a
stranger and a fabricated bounce. `docs/design/prior-art.md` records a comparable
project that checks only that ``TopicArn`` matches a configured value; but
``TopicArn`` is a field *in the request body*, and topic ARNs are not secrets.
Anyone who learns one can invent complaints against any address they like, and
a complaint rate is the number AWS suspends accounts over.

Two independent checks, and the order matters:

1. **The certificate URL must be AWS's**, verified *before* anything is
   fetched. ``SigningCertURL`` is attacker-supplied, so fetching it first and
   asking questions later turns this endpoint into a server-side request
   forgery gadget that will fetch any URL on the internet - or on the private
   network the instance happens to sit in.
2. **The RSA signature must verify** over SNS's canonical string, using the
   public key from that certificate.

The same host check guards ``SubscribeURL``, which a forged
``SubscriptionConfirmation`` would otherwise use the same way.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import load_pem_x509_certificate
from seskit_core.logging import get_logger

logger = get_logger(__name__)

#: The only hosts a certificate or a confirmation URL may live on. Anchored at
#: both ends, so ``sns.us-east-1.amazonaws.com.evil.test`` does not match - the
#: usual way this check is got wrong.
AWS_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")

#: Which fields SNS signs, in this order. A notification and a confirmation are
#: signed over different sets, and using the wrong one simply fails to verify.
NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
CONFIRMATION_FIELDS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)

#: SignatureVersion 1 signs with SHA-1, version 2 with SHA-256. SHA-1 is here
#: because SNS uses it, not as a preference - a verifier that refused it would
#: refuse most real topics, and the security of this check rests on the RSA
#: key rather than on the digest resisting collision by a party who would have
#: to control AWS's signing input to exploit one. An unknown
#: version is refused rather than guessed at: picking the wrong digest for a
#: version we do not know would reject valid messages, and defaulting
#: permissively would accept whatever an attacker declares.
_DIGESTS: dict[str, Any] = {"1": hashes.SHA1, "2": hashes.SHA256}

#: Certificates fetched so far, keyed by URL. SNS rotates them rarely and the
#: alternative is an outbound HTTPS round trip on every single notification.
#: Only URLs that already passed the host check ever reach this.
_CERTIFICATE_CACHE: dict[str, bytes] = {}

#: A ceiling so a stream of distinct (valid, AWS-hosted) URLs cannot grow this
#: without bound. Far above the handful of real signing certificates.
_CACHE_LIMIT = 32


class SignatureError(Exception):
    """The notification could not be proved to come from SNS.

    One exception for every reason, deliberately. The caller answers 403 and
    says nothing more: distinguishing "bad host" from "bad signature" in a
    response tells whoever is probing which half to work on next.
    """


def assert_aws_url(url: str) -> str:
    """Refuse any URL that is not an HTTPS AWS SNS endpoint.

    Called *before* the URL is fetched, which is the entire point. Both the
    signing certificate and a subscription confirmation are attacker-controlled
    fields, and either would happily point at ``http://169.254.169.254/`` or at
    something inside the network this instance runs in.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SignatureError("Malformed URL.") from exc

    if parsed.scheme != "https":
        raise SignatureError("URL is not HTTPS.")
    if not parsed.hostname or not AWS_SNS_HOST.match(parsed.hostname):
        raise SignatureError("URL is not an AWS SNS endpoint.")
    return url


def canonical_string(message: dict[str, Any]) -> bytes:
    """The exact bytes SNS signed.

    Each signed field as ``name\\nvalue\\n``, in SNS's order, skipping fields
    that are absent - ``Subject`` is optional, and including it as an empty
    string produces a string that will never verify.
    """
    message_type = str(message.get("Type") or "")
    fields = NOTIFICATION_FIELDS if message_type == "Notification" else CONFIRMATION_FIELDS

    parts = []
    for name in fields:
        value = message.get(name)
        if value is None:
            continue
        parts.append(f"{name}\n{value}\n")

    return "".join(parts).encode("utf-8")


async def verify(
    message: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Prove this message came from SNS, or raise.

    Raises :class:`SignatureError` and nothing else, so a caller cannot
    accidentally treat a verification failure as a transient error and retry
    its way into accepting it.
    """
    signature_b64 = message.get("Signature")
    if not signature_b64:
        raise SignatureError("No signature.")

    version = str(message.get("SignatureVersion") or "")
    digest = _DIGESTS.get(version)
    if digest is None:
        raise SignatureError("Unsupported signature version.")

    cert_url = assert_aws_url(str(message.get("SigningCertURL") or ""))

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise SignatureError("Signature is not valid base64.") from exc

    public_key = _public_key(await _certificate(cert_url, client=client))

    try:
        public_key.verify(
            signature,
            canonical_string(message),
            padding.PKCS1v15(),
            digest(),
        )
    except InvalidSignature as exc:
        raise SignatureError("Signature does not match.") from exc


def _public_key(pem: bytes) -> rsa.RSAPublicKey:
    try:
        certificate = load_pem_x509_certificate(pem)
    except Exception as exc:
        raise SignatureError("Certificate could not be read.") from exc

    key = certificate.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        # SNS signs with RSA. Anything else means the certificate is not the
        # one we think it is, whatever URL it came from.
        raise SignatureError("Certificate is not an RSA certificate.")
    return key


async def _certificate(url: str, *, client: httpx.AsyncClient | None = None) -> bytes:
    """Fetch the signing certificate, or take it from the cache.

    The URL has already passed :func:`assert_aws_url`; nothing else calls this.
    """
    cached = _CERTIFICATE_CACHE.get(url)
    if cached is not None:
        return cached

    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=5.0) as owned:
                response = await owned.get(url)
        response.raise_for_status()
    except Exception as exc:
        raise SignatureError("Certificate could not be fetched.") from exc

    pem = response.content
    if len(_CERTIFICATE_CACHE) >= _CACHE_LIMIT:
        _CERTIFICATE_CACHE.clear()
    _CERTIFICATE_CACHE[url] = pem
    return pem


async def confirm_subscription(
    subscribe_url: str, *, client: httpx.AsyncClient | None = None
) -> None:
    """Complete the SNS handshake by fetching the confirmation URL.

    The host check runs first and is not optional. A forged
    ``SubscriptionConfirmation`` naming an internal address is precisely how
    this endpoint would become an SSRF gadget, and the signature check the
    caller has already done does not remove the need for it - it is the second
    lock on the same door.
    """
    url = assert_aws_url(subscribe_url)

    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=5.0) as owned:
                response = await owned.get(url)
        response.raise_for_status()
    except Exception as exc:
        raise SignatureError("Subscription could not be confirmed.") from exc

    logger.info("sns_subscription_confirmed")
