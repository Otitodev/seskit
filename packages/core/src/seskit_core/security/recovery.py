"""Recovery codes and the operator's recovery key (§7, §22).

A forgotten dashboard password cannot be solved with email: a fresh instance
may be unable to send any until somebody can sign in and connect AWS, and that
is when a password gets forgotten. It cannot be solved with a shell either,
because the platforms people deploy to by pushing to Git do not give one. So the
way back in is something the account holder already holds.

**The code.** Sixteen characters from a 32 character alphabet is 80 bits, shown
as ``K7QD-4M2X-9PHB-TWE6``. It is shown once, at sign-up, and stored only as a
hash. Using it replaces it: a recovery code that survived being spent would be a
password with no expiry.

**SHA-256, not Argon2id,** for the reason ``api_keys.py`` gives at length and
which holds here for the same arithmetic: Argon2 makes a low-entropy secret
expensive to guess, and 80 random bits are not guessable. The attempt limit in
``throttle.py`` is what bounds an online guess, and an offline one against a
stolen database is 2^80 hashes.

**The alphabet** leaves out the characters people misread - ``I``, ``L``, ``O``
and ``U`` - and :func:`normalise_code` maps the look-alikes onto what was meant,
so a code copied by eye from paper or typed in lowercase still works. It also
drops spaces and hyphens, which chat clients and password managers add and
remove freely.

**The key** is a different thing: one long secret the *operator* puts in the
deployment's environment as ``RECOVERY_KEY``. It exists because a recovery code
only helps an account that has one, and every account created before this
feature does not. Whoever controls the environment controls the instance, so
accepting it as proof of ownership grants nothing they did not already hold.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from seskit_core.config import MIN_RECOVERY_KEY_LENGTH

#: Crockford's base32: digits and letters without ``I``, ``L``, ``O`` and ``U``.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Characters of entropy in a code. 16 x 5 bits = 80 bits.
CODE_LENGTH = 16

#: Size of the hyphen-separated groups, for reading aloud and copying by eye.
GROUP_SIZE = 4

#: What a mistyped character is most likely to have been.
_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"})


__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "MIN_RECOVERY_KEY_LENGTH",
    "code_matches",
    "generate_code",
    "hash_code",
    "key_matches",
    "normalise_code",
]


def generate_code() -> str:
    """A new recovery code, grouped for display.

    ``secrets`` rather than ``random``, as for every credential here.
    """
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + GROUP_SIZE] for i in range(0, CODE_LENGTH, GROUP_SIZE))


def normalise_code(value: str) -> str:
    """What a person typed, reduced to the form that is hashed.

    Uppercase, without separators, with look-alikes resolved. Applied to both
    sides - once when the code is issued, once when it is presented - so the two
    cannot disagree about what counts as the same code.
    """
    kept = "".join(ch for ch in value.upper() if ch.isalnum())
    return kept.translate(_LOOKALIKES)


def hash_code(code: str) -> str:
    """The SHA-256 hex digest stored for a code."""
    return hashlib.sha256(normalise_code(code).encode("utf-8")).hexdigest()


def code_matches(code: str, stored_hash: str | None) -> bool:
    """Whether ``code`` is the one ``stored_hash`` was made from.

    Constant-time. Takes a missing hash and answers False after the same work,
    so an account with no code and one with the wrong code look the same.
    """
    expected = stored_hash if stored_hash is not None else "0" * 64
    matched = hmac.compare_digest(hash_code(code), expected)
    return matched and stored_hash is not None


def key_matches(presented: str, configured: str) -> bool:
    """Whether ``presented`` is the operator's recovery key.

    Always False when no key is configured, whatever was presented - an empty
    string must not match an empty setting. Compared as bytes: ``compare_digest``
    raises ``TypeError`` on non-ASCII ``str``, which would turn a hostile form
    field into a 500.
    """
    if not configured:
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), configured.encode("utf-8"))
