"""One-click unsubscribe links (RFC 8058, §31 Phase 11).

A mail client that shows an "Unsubscribe" button next to the sender's name
gets it from the ``List-Unsubscribe`` header. Making that button work is worth
more than politeness: a recipient who cannot find it presses *Report spam*
instead, and a complaint costs a reputation point that an unsubscribe does not.

**The link has to work with no session and no account.** The recipient is not a
SESKit user - they may not know SESKit exists - so authorisation cannot come
from a cookie. It comes from the link itself, which carries who it is for and a
signature proving SESKit wrote it.

A token is::

    base64url(email_id:address) . hmac-sha256 hex

Both halves are needed. The signature alone cannot be reversed into an address,
so the payload has to travel too; the payload alone would let anyone unsubscribe
anyone by editing a URL, so it has to be signed.

**The key is derived per project rather than being the instance secret.** The
secret signs the project id, and the result signs the token. It costs one extra
HMAC and means a token forged against one project's scheme cannot be replayed
against another, and that the instance secret never directly signs a string an
attacker chose. Same shape as `security/webhooks.py`, and for the same reason.

There is deliberately no expiry. A link in a message somebody kept for two
years should still work: an expired unsubscribe link produces exactly the
complaint the header exists to prevent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

#: Separates the payload from its signature. Not present in base64url output
#: or in hex, so the split is unambiguous.
TOKEN_SEPARATOR = "."  # noqa: S105 - a separator, not a secret

#: What RFC 8058 requires in the body of the one-click POST, and the value of
#: the header that advertises support for it.
ONE_CLICK = "List-Unsubscribe=One-Click"

LIST_UNSUBSCRIBE_HEADER = "List-Unsubscribe"
LIST_UNSUBSCRIBE_POST_HEADER = "List-Unsubscribe-Post"


def _project_key(secret: str, project_id: str) -> bytes:
    """The signing key for one project's links."""
    return hmac.new(secret.encode("utf-8"), project_id.encode("utf-8"), hashlib.sha256).digest()


def _payload(email_id: str, address: str) -> str:
    raw = f"{email_id}:{address}".encode()
    # Unpadded, because "=" in a URL path is legal but invites a proxy or a mail
    # client to re-encode it, and a re-encoded token is a token that stops
    # verifying.
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(secret: str, *, project_id: str, payload: str) -> str:
    return hmac.new(
        _project_key(secret, project_id), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()


def unsubscribe_token(secret: str, *, project_id: str, email_id: str, address: str) -> str:
    """A token identifying one recipient of one message."""
    payload = _payload(email_id, address)
    return f"{payload}{TOKEN_SEPARATOR}{_sign(secret, project_id=project_id, payload=payload)}"


def read_token(token: str) -> tuple[str, str] | None:
    """Read ``(email_id, address)`` out of a token **without verifying it**.

    Separate from verification because the two need different things: the
    project id that keys the signature is only known once the message has been
    looked up, and the message can only be looked up once the token has been
    read. So this runs first, on untrusted input, and the caller must call
    :func:`token_matches` before acting on what it returns.

    Everything it can hand back is either a well-formed id, which resolves to at
    most one indexed row, or nothing.
    """
    payload, separator, signature = token.partition(TOKEN_SEPARATOR)
    if not separator or not payload or not signature:
        return None

    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None

    email_id, colon, address = raw.partition(":")
    if not colon or not email_id or not address:
        return None
    return email_id, address


def token_matches(secret: str, *, project_id: str, token: str) -> bool:
    """Whether this instance really issued this token for this project.

    Constant time, for the reason `security/webhooks.py` gives: a byte-at-a-time
    comparison hands the correct signature to anyone willing to make enough
    attempts, and here that would be a way to unsubscribe an address of the
    attacker's choosing.
    """
    payload, separator, signature = token.partition(TOKEN_SEPARATOR)
    if not separator:
        return False
    expected = _sign(secret, project_id=project_id, payload=payload)
    return hmac.compare_digest(expected, signature)
