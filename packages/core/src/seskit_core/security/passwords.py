"""Password hashing (§22).

Argon2id at OWASP's recommended parameters, via ``pwdlib``. ``passlib`` is the
older default but is effectively unmaintained - PyPI's own Warehouse has an open
issue about moving off it - so a new project should not start there.
"""

from __future__ import annotations

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

# OWASP Password Storage Cheat Sheet, Argon2id:
#   memory 46 MiB, 1 iteration, 1 degree of parallelism.
# argon2-cffi expresses memory in KiB, hence 47104.
#
# These are stored inside every hash, so raising them later does not invalidate
# existing passwords - `needs_rehash` reports which ones are behind, and they are
# upgraded transparently at the next successful login.
ARGON2_MEMORY_COST_KIB = 47104
ARGON2_TIME_COST = 1
ARGON2_PARALLELISM = 1

#: A password nobody can hold. Used to burn the same CPU time when an email is
#: unknown, so response timing cannot be used to enumerate accounts.
_DUMMY_PASSWORD = "seskit-timing-equaliser"  # noqa: S105 - not a credential

_hasher = PasswordHash(
    (
        Argon2Hasher(
            memory_cost=ARGON2_MEMORY_COST_KIB,
            time_cost=ARGON2_TIME_COST,
            parallelism=ARGON2_PARALLELISM,
        ),
    )
)


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The plaintext is never stored or logged."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Whether ``password`` matches the stored hash."""
    return _hasher.verify(password, password_hash)


def verify_and_update(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify, and return a fresh hash when the stored one uses old parameters.

    Lets the cost parameters be raised over time: an account whose hash predates
    the change is re-hashed on its next successful login, with nobody locked out
    and no forced reset.
    """
    return _hasher.verify_and_update(password, password_hash)


def burn_dummy_hash() -> None:
    """Hash a throwaway password to match the cost of a real verification.

    Called on the unknown-email path at login. Without it, a missing account
    answers measurably faster than a wrong password, which turns the login form
    into an account-enumeration oracle.
    """
    _hasher.hash(_DUMMY_PASSWORD)
