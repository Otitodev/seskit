"""Encrypting a stored AWS secret access key (§31 Phase 14).

SESKit now holds an access key per project rather than reading credentials
from the environment it runs in, so it holds something worth stealing. This
module is the whole of what protects it at rest.

**What this does protect against.** A database dump - a backup that leaves the
building, a snapshot on someone's laptop, a `pg_dump` in a support ticket, a
replica with looser access. In every one of those the ciphertext is useless
without `SECRET_KEY`, which lives in the environment and not in the database.

**What it does not.** A compromised host. The key is derived from `SECRET_KEY`,
which the process must be able to read in order to send mail at all, so
anything that can read the application's environment can decrypt every stored
credential. Encryption at rest is not a defence against an attacker who is
already inside; it is a defence against the copy that walks out. The
documentation says this in the same words, because a reader who believes
otherwise will make worse decisions than one who was told plainly.

**The key is derived, not configured.** Requiring a second secret would add a
step to a setup this phase exists to shorten, and an operator who sets one and
loses it has lost every stored credential just as surely. HKDF domain-separates
it from every other use of `SECRET_KEY` - sessions, CSRF, webhook signatures,
unsubscribe tokens - so no two of them share key material even though they
share a source.

The consequence has to be stated wherever rotation is: **rotating `SECRET_KEY`
makes every stored credential unreadable**, and each project must be connected
again. That is a real cost of deriving rather than configuring, and it is
recorded here rather than discovered.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: Domain separation. Every other thing derived from ``SECRET_KEY`` uses a
#: different construction or a different label, so recovering one key never
#: yields another. The version suffix exists so a future change of scheme can
#: be told apart from this one rather than silently producing garbage.
HKDF_INFO = b"seskit.aws-credentials.v1"

#: Fernet's key size. Not a choice - it is what the construction takes.
KEY_BYTES = 32


class CredentialsUnreadable(Exception):
    """A stored credential could not be decrypted.

    Almost always one thing: ``SECRET_KEY`` has changed since the credential
    was stored. It can also mean the ciphertext was truncated or edited in the
    database.

    Carries no detail about what failed and never the value it was given.
    There is nothing an attacker could learn from the difference between "wrong
    key" and "corrupt token", and a caller has the same job either way: tell
    the operator to connect the project again.
    """


def _fernet(secret_key: str) -> Fernet:
    """The cipher for one instance's stored credentials.

    ``salt=None`` deliberately. HKDF is normally salted, but a salt would have
    to be stored beside the ciphertext and recovered to decrypt - and the
    property that matters here is that the same ``SECRET_KEY`` always derives
    the same key, on every process, without coordination. The input is a
    high-entropy secret rather than a password, which is the case where a salt
    buys least.
    """
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=HKDF_INFO,
    ).derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_secret_access_key(value: str, *, secret_key: str) -> str:
    """Encrypt one secret access key for storage.

    Returns text rather than bytes so the column can be a plain ``Text`` and a
    row stays readable in ``psql`` as obviously-encrypted rather than as a
    binary blob somebody has to decode before they can tell what it is.

    Fernet picks a fresh IV per call, so encrypting the same key twice gives
    two different ciphertexts. That is the point: identical stored values must
    not reveal that two projects share a key.
    """
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret_access_key(token: str, *, secret_key: str) -> str:
    """Recover a stored secret access key.

    Raises :class:`CredentialsUnreadable` for anything that does not decrypt
    and authenticate. Fernet is authenticated, so a token edited in the
    database fails here rather than producing a plausible-looking wrong key
    that would fail much later, against AWS, as a confusing permissions error.
    """
    try:
        return _fernet(secret_key).decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise CredentialsUnreadable(
            "This project's stored AWS credentials could not be read. "
            "They were encrypted with a different SECRET_KEY - connect the "
            "project again to store a new access key."
        ) from exc
