"""Password hashing (§22)."""

from __future__ import annotations

import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from seskit_core.security.passwords import (
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    burn_dummy_hash,
    hash_password,
    verify_and_update,
    verify_password,
)


def test_hash_does_not_contain_the_password() -> None:
    """The obvious thing, asserted anyway - it is the whole point of §22."""
    assert "correct horse battery staple" not in hash_password("correct horse battery staple")


def test_hash_is_argon2id() -> None:
    assert hash_password("whatever").startswith("$argon2id$")


def test_correct_password_verifies() -> None:
    assert verify_password("s3cret", hash_password("s3cret")) is True


def test_wrong_password_does_not_verify() -> None:
    assert verify_password("wrong", hash_password("s3cret")) is False


def test_hashes_are_salted() -> None:
    """The same password must not produce the same hash twice.

    Without a per-hash salt, identical passwords are visible as identical rows
    and a single rainbow table breaks every account at once.
    """
    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize(
    "password",
    ["", " ", "a", "unicode-café-🔑", "x" * 1000, "tab\tand\nnewline"],
)
def test_awkward_passwords_round_trip(password: str) -> None:
    assert verify_password(password, hash_password(password)) is True


def test_parameters_match_the_owasp_recommendation() -> None:
    """46 MiB, 1 iteration, 1 lane.

    Pinned so the cost cannot be quietly lowered - a weaker setting would still
    pass every other test in this file.
    """
    assert ARGON2_MEMORY_COST_KIB == 47104
    assert ARGON2_TIME_COST == 1
    assert ARGON2_PARALLELISM == 1


def test_verify_and_update_leaves_a_current_hash_alone() -> None:
    valid, updated = verify_and_update("s3cret", hash_password("s3cret"))

    assert valid is True
    assert updated is None


def test_verify_and_update_rehashes_a_weaker_hash() -> None:
    """Cost parameters can be raised later without locking anyone out.

    A hash made with older settings is replaced at the next successful login,
    with no forced reset.
    """
    weak = PasswordHash((Argon2Hasher(memory_cost=8192, time_cost=1, parallelism=1),))
    old_hash = weak.hash("s3cret")

    valid, updated = verify_and_update("s3cret", old_hash)

    assert valid is True
    assert updated is not None
    assert updated != old_hash
    assert verify_password("s3cret", updated) is True


def test_verify_and_update_rejects_a_wrong_password() -> None:
    valid, updated = verify_and_update("wrong", hash_password("s3cret"))

    assert valid is False
    assert updated is None


def test_burn_dummy_hash_runs() -> None:
    """Called on the unknown-email path so a missing account is not faster.

    Only its cost matters, so there is nothing to assert but that it works.
    """
    burn_dummy_hash()
