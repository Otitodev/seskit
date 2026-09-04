"""Signing webhook deliveries (§16).

Pure - no database, no network. What is being protected is a customer's
decision to act on a webhook: a forged `email.bounced` means suppressing an
address the attacker chose.

The test that matters is `test_the_timestamp_is_inside_the_signature`. Signing
the body alone produces a signature that stays valid forever, so anyone who
captures one request can replay it unchanged and it verifies every time. That is
the failure `docs/design/prior-art.md` warns about, and it is invisible in every other
test here - a body-only scheme would pass all of them.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from seskit_core.security.webhooks import (
    DEFAULT_TOLERANCE_SECONDS,
    SECRET_PREFIX,
    SIGNATURE_VERSION,
    generate_secret,
    sign,
    signed_payload,
    verify,
)

SECRET = "whsec_test_secret_value"
BODY = b'{"id":"evt_1","type":"email.delivered"}'
TIMESTAMP = 1756800000


# ------------------------------------------------------------------ secrets ---


def test_a_secret_is_prefixed_and_long() -> None:
    secret = generate_secret()

    assert secret.startswith(SECRET_PREFIX)
    # 32 random bytes, URL-safe encoded, plus the prefix.
    assert len(secret) > 40


def test_two_secrets_differ() -> None:
    """Obvious, and worth stating: a fixed secret would make every endpoint on
    every instance share one, which is the same as having none.
    """
    assert generate_secret() != generate_secret()


# ---------------------------------------------------------------- the scheme ---


def test_the_signed_string_is_timestamp_dot_body() -> None:
    """The one part a customer has to reproduce byte-for-byte. Asserted
    directly rather than inferred from a signature that happens to match.
    """
    assert signed_payload(TIMESTAMP, BODY) == b"1756800000." + BODY


def test_the_signature_carries_its_version() -> None:
    """So a future scheme can be introduced alongside this one rather than in
    place of it. Without the prefix the only migration is a flag day.
    """
    signature, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)

    assert signature.startswith(f"{SIGNATURE_VERSION}=")


def test_the_signature_is_a_plain_hmac_a_customer_can_reproduce() -> None:
    """Computed here the long way, with nothing imported from the module under
    test but the constants. If this passes, the README's instructions are
    implementable from the README alone.
    """
    expected = hmac.new(SECRET.encode(), b"1756800000." + BODY, hashlib.sha256).hexdigest()

    signature, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)

    assert signature == f"v1={expected}"


def test_the_timestamp_is_inside_the_signature() -> None:
    """**The test that matters.**

    Change only the timestamp and the signature must change. If it does not,
    the timestamp is being sent beside the signature rather than covered by it,
    and a captured request can be replayed forever with a fresh one - the exact
    failure docs/design/prior-art.md identifies. Every other test in this file would
    still pass.
    """
    first, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)
    second, _ = sign(SECRET, BODY, timestamp=TIMESTAMP + 1)

    assert first != second


def test_signing_returns_the_timestamp_it_used() -> None:
    """The caller has to send the same value it signed. Deriving it twice would
    eventually straddle a second boundary and ship a signature that cannot
    verify - rare, undebuggable, and entirely avoidable.
    """
    _, when = sign(SECRET, BODY)

    assert isinstance(when, int)
    assert when > 1_700_000_000


# --------------------------------------------------------------- verification ---


def test_a_genuine_delivery_verifies() -> None:
    signature, when = sign(SECRET, BODY)

    assert verify(SECRET, BODY, signature=signature, timestamp=when) is True


def test_a_tampered_body_is_refused() -> None:
    """The forgery this exists to stop: a plausible event, unsigned."""
    signature, when = sign(SECRET, BODY)
    tampered = json.dumps({"id": "evt_1", "type": "email.bounced"}).encode()

    assert verify(SECRET, tampered, signature=signature, timestamp=when) is False


def test_the_wrong_secret_is_refused() -> None:
    signature, when = sign(SECRET, BODY)

    assert verify("whsec_someone_elses", BODY, signature=signature, timestamp=when) is False


def test_a_replay_outside_the_tolerance_is_refused() -> None:
    """A perfectly valid signature on a stale timestamp is what a replay looks
    like. Refused before the signature is even computed.
    """
    signature, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)

    assert (
        verify(
            SECRET,
            BODY,
            signature=signature,
            timestamp=TIMESTAMP,
            now=TIMESTAMP + DEFAULT_TOLERANCE_SECONDS + 1,
        )
        is False
    )


def test_a_delivery_inside_the_tolerance_is_accepted() -> None:
    """Clocks are not identical and networks are not instant."""
    signature, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)

    assert (
        verify(
            SECRET,
            BODY,
            signature=signature,
            timestamp=TIMESTAMP,
            now=TIMESTAMP + DEFAULT_TOLERANCE_SECONDS - 1,
        )
        is True
    )


def test_a_timestamp_from_the_future_is_also_refused() -> None:
    """Symmetric on purpose. A receiver whose clock is behind would otherwise
    accept anything dated far enough ahead.
    """
    signature, _ = sign(SECRET, BODY, timestamp=TIMESTAMP)

    assert (
        verify(
            SECRET,
            BODY,
            signature=signature,
            timestamp=TIMESTAMP,
            now=TIMESTAMP - DEFAULT_TOLERANCE_SECONDS - 1,
        )
        is False
    )


@pytest.mark.parametrize(
    "signature",
    ["", "v1=", "nonsense", "3f7a", "v2=3f7a", "v1=" + "0" * 64],
)
def test_a_malformed_or_wrong_signature_is_refused(signature: str) -> None:
    assert verify(SECRET, BODY, signature=signature, timestamp=TIMESTAMP, now=TIMESTAMP) is False
