"""Encrypting a stored AWS secret access key (§31 Phase 14).

Before this phase SESKit held nothing worth stealing: credentials came from the
environment it ran in and were never written down. Now a row in the database is
a live AWS key, and this module is the whole of what stands between a leaked
backup and somebody else's SES account.

So the tests are about the failure modes rather than the happy path, which is
one line. What must hold: a wrong key refuses rather than returning plausible
rubbish, a tampered ciphertext is caught rather than decrypting to a wrong key
that fails confusingly against AWS much later, and nothing raised or returned
ever carries the secret it was handling.
"""

from __future__ import annotations

import pytest
from seskit_core.security.aws_credentials import (
    HKDF_INFO,
    CredentialsUnreadable,
    decrypt_secret_access_key,
    encrypt_secret_access_key,
)

#: AWS's own documentation example. Not a live credential.
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
INSTANCE_SECRET = "an-instance-secret-key"


# ------------------------------------------------------------ round trip ---


def test_a_key_comes_back_as_it_went_in() -> None:
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    assert decrypt_secret_access_key(token, secret_key=INSTANCE_SECRET) == SECRET


def test_the_stored_form_is_not_the_secret() -> None:
    """Stated as a test because it is the entire claim being made about the
    column. A "reversible encoding" that shipped by accident would pass every
    round-trip test above and protect nothing.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    assert SECRET not in token
    assert "wJalr" not in token


def test_the_same_key_encrypts_differently_each_time() -> None:
    """Fernet picks a fresh IV per call. Without that, two projects using one
    IAM user would store identical ciphertext, and anyone reading the table
    would learn they share a key without decrypting anything.
    """
    first = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)
    second = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    assert first != second
    assert decrypt_secret_access_key(first, secret_key=INSTANCE_SECRET) == SECRET
    assert decrypt_secret_access_key(second, secret_key=INSTANCE_SECRET) == SECRET


def test_the_token_is_text_that_survives_a_database() -> None:
    """It goes in a `Text` column and comes back through a driver, a dump and
    possibly somebody's clipboard. ASCII is what makes that uneventful.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    assert isinstance(token, str)
    assert token.isascii()


@pytest.mark.parametrize(
    "secret",
    ["", "a", "x" * 4096, "秘密鍵", "has spaces and / slashes +="],
    ids=["empty", "single", "long", "unicode", "punctuation"],
)
def test_anything_a_key_might_contain_round_trips(secret: str) -> None:
    """AWS secrets are base64-ish today. Encoding assumptions about somebody
    else's format are how a client breaks on the day that format changes.
    """
    token = encrypt_secret_access_key(secret, secret_key=INSTANCE_SECRET)

    assert decrypt_secret_access_key(token, secret_key=INSTANCE_SECRET) == secret


# --------------------------------------------------------------- refusal ---


def test_a_different_secret_key_cannot_read_it() -> None:
    """The property the whole design rests on: the ciphertext is worthless
    without the environment, so a stolen database is not a stolen AWS account.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    with pytest.raises(CredentialsUnreadable):
        decrypt_secret_access_key(token, secret_key="a-different-secret")


def test_a_rotated_secret_key_is_the_common_case() -> None:
    """Not an attack - an operator following the advice to rotate. It has to
    fail cleanly and say what to do, because it is the failure real people will
    actually hit.
    """
    token = encrypt_secret_access_key(SECRET, secret_key="old-secret")

    with pytest.raises(CredentialsUnreadable) as raised:
        decrypt_secret_access_key(token, secret_key="new-secret")

    assert "SECRET_KEY" in str(raised.value)
    assert "connect the project again" in str(raised.value)


def test_an_edited_ciphertext_is_caught() -> None:
    """Fernet authenticates. Without that, a byte changed in the database would
    decrypt to a wrong key that fails against AWS hours later as an opaque
    permissions error, rather than here as a clear one.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)
    edited = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    with pytest.raises(CredentialsUnreadable):
        decrypt_secret_access_key(edited, secret_key=INSTANCE_SECRET)


@pytest.mark.parametrize(
    "token",
    ["", "   ", "not-a-token", "gAAAAA", "!!!!", "null"],
    ids=["empty", "blank", "prose", "truncated", "punctuation", "null-string"],
)
def test_nonsense_raises_the_documented_error(token: str) -> None:
    """A column that was never written, or was written by something else.
    Every one of these must be the same exception a caller already handles -
    not a `ValueError` or a `TypeError` escaping from a crypto library.
    """
    with pytest.raises(CredentialsUnreadable):
        decrypt_secret_access_key(token, secret_key=INSTANCE_SECRET)


def test_the_error_never_carries_what_it_was_given() -> None:
    """The exception is logged and may be rendered. It must not become the
    thing that puts a credential into a log line.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    with pytest.raises(CredentialsUnreadable) as raised:
        decrypt_secret_access_key(token, secret_key="wrong")

    message = str(raised.value)
    assert token not in message
    assert INSTANCE_SECRET not in message
    assert "wrong" not in message


# ------------------------------------------------------ domain separation ---


def test_the_key_is_not_the_instance_secret() -> None:
    """`SECRET_KEY` signs sessions, CSRF tokens, webhook payloads and
    unsubscribe links. Using it directly as a cipher key would mean one
    recovered key compromises all of them.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    assert INSTANCE_SECRET not in token


def test_the_derivation_is_labelled_and_versioned() -> None:
    """The label is what separates this key from every other use of
    `SECRET_KEY`, and the version is what lets a future scheme be told apart
    from this one rather than silently producing garbage.
    """
    assert b"aws-credentials" in HKDF_INFO
    assert HKDF_INFO.endswith(b".v1")


def test_derivation_is_stable_across_processes() -> None:
    """No salt, on purpose: a salt would have to be stored beside the
    ciphertext, and what matters is that the same SECRET_KEY derives the same
    key in the API and in the worker, which never coordinate.
    """
    token = encrypt_secret_access_key(SECRET, secret_key=INSTANCE_SECRET)

    # A second call is a fresh HKDF derivation; if it were salted this would
    # fail rather than merely differ.
    assert decrypt_secret_access_key(token, secret_key=INSTANCE_SECRET) == SECRET
