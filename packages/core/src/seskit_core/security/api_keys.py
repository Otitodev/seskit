"""API key generation and hashing (§7, §22).

**SHA-256, not Argon2id.** That looks wrong next to ``passwords.py`` and is
deliberate:

- Argon2 exists to make a low-entropy human password expensive to guess. An API
  key here is 256 bits from ``secrets`` - there is nothing to guess, so the cost
  buys no security.
- It would be charged on *every* API request. At 46 MiB and tens of
  milliseconds a call, that is a denial of service we inflict on ourselves.
- Argon2 salts each hash, so the same key hashes differently every time and a
  key cannot be looked up *by* its hash. SHA-256 is deterministic, so
  ``WHERE hashed_key = :h`` hits a unique index and returns one row.

That last point is a convenience, not the argument, and it is worth being
precise about because there is a well-known design that defeats it: issue the
key as ``prefix_lookupid_secret``, index the lookup id, and verify the secret
with a salted KDF against that one row. It works, and other projects in this
space use it.

We still do not, because the *first* reason is the real one. A KDF is machinery
for making a low-entropy secret expensive to guess. There is no guessing to
frustrate here, so the only thing a KDF adds to a 256-bit random token is
latency on every authenticated request - and the extra column and split-parsing
to go with it.

The lookup is not constant-time, but what leaks is information about a 256-bit
secret the caller must already know to make the query at all.
"""

from __future__ import annotations

import hashlib
import secrets

#: Marks a SESKit secret key. One key type: a ``sk_live_`` prefix would imply a
#: ``sk_test_`` mode that a self-hosted instance does not have.
KEY_PREFIX = "sk_"

#: Bytes of randomness behind each key. 32 bytes is 256 bits, which
#: ``token_urlsafe`` renders as 43 URL-safe characters.
KEY_ENTROPY_BYTES = 32

#: How much of the raw key is kept in clear for display, including ``sk_``.
#: Enough to tell two keys apart in a list, useless as a credential.
DISPLAY_PREFIX_LENGTH = 11

#: Bearer is the scheme in §7's example: ``Authorization: Bearer sk_...``.
BEARER_SCHEME = "bearer"


def generate_key() -> str:
    """Return a new raw API key.

    ``secrets`` rather than ``random``: the latter is a Mersenne Twister whose
    future output is derivable from past output, which for a credential is
    fatal.

    The return value is the only time this string exists. It is shown to the
    user once and then only its hash is kept.
    """
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_ENTROPY_BYTES)}"


def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest stored for ``raw_key``.

    Deterministic and unsalted on purpose - see the module docstring. Salting
    would make the stored value unsearchable, which is the whole point of it.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def display_prefix(raw_key: str) -> str:
    """The portion of a key kept in clear, e.g. ``sk_3nK9vQ2m``."""
    return raw_key[:DISPLAY_PREFIX_LENGTH]


def looks_like_key(value: str) -> bool:
    """Whether a string is shaped like one of our keys.

    Lets an obviously malformed credential be rejected before it is hashed and
    looked up. Not a security control - a well-formed forgery still fails
    verification - just a way to avoid a pointless round trip.
    """
    return value.startswith(KEY_PREFIX) and len(value) > len(KEY_PREFIX)


def parse_authorization(header: str | None) -> str | None:
    """Pull the raw key out of an ``Authorization`` header.

    Returns ``None`` for anything that is not ``Bearer sk_...`` so the caller
    has one shape to handle rather than several failure modes.
    """
    if not header:
        return None

    scheme, _, credential = header.partition(" ")
    if scheme.lower() != BEARER_SCHEME:
        return None

    credential = credential.strip()
    return credential if looks_like_key(credential) else None
