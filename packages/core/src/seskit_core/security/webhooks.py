"""Signing webhook deliveries (§16).

A customer receiving a webhook has to answer one question before acting on it:
did this really come from my SESKit instance, or from anyone who guessed the
URL? Acting on a forged `email.bounced` means suppressing an address the
attacker chose.

**The timestamp is signed with the body, not sent beside it.** This is the whole
design, and it is the thing `docs/design/prior-art.md` calls out. Signing the body
alone gives a signature that stays valid forever: anyone who captures one
request can replay it unchanged, indefinitely, and each replay verifies. Binding
the timestamp into the signed string means a replay carries the *original*
timestamp, and the receiver can reject anything too old.

So the signed string is::

    {timestamp}.{body}

and what goes on the wire is::

    X-SESKit-Timestamp: 1756800000
    X-SESKit-Signature: v1=3f7a...

**The `v1=` prefix earns its keep later.** When the scheme changes - a different
digest, a different string to sign - the header can carry both for a while and
receivers can accept either. Without the prefix the only migration available is
a flag day.

Everything here is deliberately implementable in ten lines in any language, from
the README alone. A signature scheme a customer cannot reimplement is a
signature scheme they will skip verifying.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

#: Marks a SESKit webhook secret, the way ``sk_`` marks an API key. Visible in
#: the dashboard and in the customer's own configuration, so it is worth being
#: recognisable when it turns up somewhere it should not.
SECRET_PREFIX = "whsec_"  # noqa: S105 - a prefix, not a secret

#: 32 bytes is 256 bits. The secret is never guessed at online - it is only ever
#: compared against - so entropy is the entire defence.
SECRET_ENTROPY_BYTES = 32

SIGNATURE_HEADER = "X-SESKit-Signature"
TIMESTAMP_HEADER = "X-SESKit-Timestamp"

#: The scheme this module implements. Sent as a prefix so a future ``v2=`` can
#: be introduced alongside rather than in place of it.
SIGNATURE_VERSION = "v1"

#: How far out of date a signature may be before a receiver should refuse it.
#: Five minutes is enough for a slow network and a clock that is not quite
#: right; it is not enough to be useful to someone replaying yesterday's
#: capture. Advisory - the receiver enforces it, which is why it is documented.
DEFAULT_TOLERANCE_SECONDS = 300


def generate_secret() -> str:
    """Return a new webhook signing secret.

    ``secrets`` rather than ``random``: the latter is a Mersenne Twister whose
    future output is derivable from its past output, which for a shared secret
    is fatal.

    Unlike an API key, this string is stored as it is returned. The customer
    needs it to verify signatures and SESKit needs it to produce them, so there
    is no version of this that can be hashed.
    """
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(SECRET_ENTROPY_BYTES)}"


def signed_payload(timestamp: int, body: bytes) -> bytes:
    """The exact bytes that get signed.

    Separated out because it is the one part a customer must reproduce
    byte-for-byte, and because the tests assert on it directly rather than
    inferring it from a signature that happens to match.
    """
    return f"{timestamp}.".encode() + body


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> tuple[str, int]:
    """Sign one delivery. Returns ``(signature_header_value, timestamp)``.

    The timestamp is returned rather than only used, because the caller has to
    send the same value it signed - deriving it twice would eventually straddle
    a second boundary and produce a signature that cannot verify.
    """
    when = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(
        secret.encode("utf-8"), signed_payload(when, body), hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}", when


def verify(
    secret: str,
    body: bytes,
    *,
    signature: str,
    timestamp: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    """Check a signature the way a customer's own code would.

    SESKit does not receive its own webhooks, so nothing in production calls
    this. It exists so the verification instructions in the README are executed
    by the test suite rather than only proofread - documented instructions that
    have never been run are worse than none.
    """
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        # Refused before the signature is even computed: a valid signature on a
        # stale timestamp is exactly what a replay looks like.
        return False

    expected, _ = sign(secret, body, timestamp=timestamp)
    # Constant time, because a byte-at-a-time comparison leaks the correct
    # signature to anyone willing to make enough attempts.
    return hmac.compare_digest(expected, signature)
