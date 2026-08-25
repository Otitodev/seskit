"""Prefixed ULID identifiers.

The format is part of the public API and cannot be changed once customers store
the values, so its properties are pinned here rather than assumed.
"""

from __future__ import annotations

import time

import pytest
from seskit_core.ids import IDPrefix, generate_id, has_prefix


@pytest.mark.parametrize("prefix", list(IDPrefix))
def test_identifier_carries_its_prefix(prefix: IDPrefix) -> None:
    assert generate_id(prefix).startswith(f"{prefix.value}_")


def test_identifiers_are_unique() -> None:
    assert len({generate_id(IDPrefix.USER) for _ in range(1000)}) == 1000


def test_identifiers_sort_by_creation_time() -> None:
    """Lexical order must match creation order.

    This is the whole reason for choosing ULIDs over UUID4: "newest first" needs
    no extra column, and inserts stay local in the index.
    """
    first = generate_id(IDPrefix.EMAIL)
    time.sleep(0.002)  # ULID timestamps have millisecond resolution
    second = generate_id(IDPrefix.EMAIL)

    assert first < second


def test_prefixes_are_stable() -> None:
    """These strings ship in the API. Changing one breaks stored customer data."""
    assert IDPrefix.USER.value == "usr"
    assert IDPrefix.PROJECT.value == "proj"
    assert IDPrefix.API_KEY.value == "key"
    assert IDPrefix.DOMAIN.value == "dom"
    assert IDPrefix.EMAIL.value == "email"
    assert IDPrefix.EVENT.value == "evt"
    assert IDPrefix.WEBHOOK.value == "wh"


def test_prefixes_are_distinct() -> None:
    values = [p.value for p in IDPrefix]

    assert len(values) == len(set(values))


def test_has_prefix_accepts_a_matching_identifier() -> None:
    assert has_prefix(generate_id(IDPrefix.PROJECT), IDPrefix.PROJECT) is True


def test_has_prefix_rejects_another_type() -> None:
    """Catches a project id passed where an email id belongs."""
    assert has_prefix(generate_id(IDPrefix.PROJECT), IDPrefix.EMAIL) is False


def test_has_prefix_is_not_fooled_by_a_shared_leading_substring() -> None:
    """The evt prefix must not match an id that merely starts with those letters."""
    assert has_prefix("evtx_01J", IDPrefix.EVENT) is False


def test_identifier_body_is_a_ulid() -> None:
    body = generate_id(IDPrefix.USER).split("_", 1)[1]

    assert len(body) == 26
    assert body.isalnum()
    assert body.isupper()
