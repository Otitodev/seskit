"""Which domain an email was sent from.

A domain's page finds its mail by `Email.from_domain`, so the one thing that
must hold is that the column always agrees with `from_address`. These need no
database.
"""

from __future__ import annotations

import pytest
from seskit_core.addressing import domain_of
from seskit_core.models import Email


@pytest.mark.parametrize(
    ("address", "domain"),
    [
        ("hello@example.com", "example.com"),
        ("Hello@Example.COM", "example.com"),
        ("Acme Support <help@mail.example.com>", "mail.example.com"),
        # The display name contains an @. The mailbox is the part in brackets.
        ('"billing@old.example" <real@example.com>', "example.com"),
        # A quoted local part may contain an @; the domain follows the last one.
        ('"odd@name"@example.com', "example.com"),
        ("hello@example.com.", "example.com"),
        ("no-domain", None),
        ("trailing@", None),
        ("", None),
    ],
)
def test_domain_of_reduces_an_address_to_its_domain(address: str, domain: str | None) -> None:
    assert domain_of(address) == domain


def _email(address: str) -> Email:
    return Email(
        project_id="proj_1",
        from_address=address,
        to_addresses=["you@example.org"],
        subject="Hello",
    )


def test_an_email_records_its_domain_when_it_is_built() -> None:
    """Many tests, and any code path, build an Email directly. None of them
    should be able to leave the domain out."""
    assert _email("Acme <hello@Example.com>").from_domain == "example.com"


def test_changing_the_address_changes_the_domain() -> None:
    email = _email("hello@example.com")

    email.from_address = "hello@other.example"

    assert email.from_domain == "other.example"


def test_an_address_with_no_domain_has_none() -> None:
    assert _email("nonsense").from_domain is None


def test_the_services_import_cold() -> None:
    """The `Email` model needs `domain_of`, and the model sits at the bottom of the
    import graph. When `domain_of` lived in `email.message`, importing
    `seskit_core.services` first raised an ImportError (message -> security ->
    models -> message) that no test hit, because every test happens to import the
    models first. A fresh interpreter is the only way to see it.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-I", "-c", "import seskit_core.services"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
