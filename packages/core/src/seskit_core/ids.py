"""Prefixed, sortable identifiers.

Every public identifier looks like ``usr_01J8XQ...`` - a short type prefix and a
ULID. The shape is fixed here because it appears in the API contract (§11 uses
``email_01J...``), in logs, and in support conversations, and it is effectively
permanent once customers have stored the values.

Why not a bare UUID4:

- **Readable.** The prefix says what the thing is. ``proj_01J...`` in a log line
  or a bug report needs no lookup to identify.
- **Sortable.** A ULID opens with a 48-bit millisecond timestamp, so identifiers
  sort by creation time. That gives better index locality than random UUIDs, and
  "newest first" needs no extra column.
- **Unguessable.** The remaining 80 bits are random, so an identifier cannot be
  walked the way a sequential integer can.
"""

from __future__ import annotations

from enum import StrEnum

from ulid import ULID


class IDPrefix(StrEnum):
    """Type prefixes.

    Declared for later phases as well, so the vocabulary lives in one place and
    two phases cannot invent competing prefixes for the same concept. These
    values are part of the public API - never change one.
    """

    USER = "usr"
    PROJECT = "proj"
    API_KEY = "key"
    AWS_CONNECTION = "aws"
    DOMAIN = "dom"
    EMAIL = "email"
    EVENT = "evt"
    WEBHOOK = "wh"
    WEBHOOK_DELIVERY = "whd"


def generate_id(prefix: IDPrefix) -> str:
    """Return a new identifier, e.g. ``usr_01J8XQ2K3M4N5P6Q7R8S9T0V1W``."""
    return f"{prefix.value}_{ULID()}"


def has_prefix(value: str, prefix: IDPrefix) -> bool:
    """Whether ``value`` is an identifier of the given type.

    Lets a well-formed identifier of the wrong kind be rejected at the edge.
    Passing a project id where an email id belongs would otherwise surface as a
    confusing empty result rather than an error.
    """
    return value.startswith(f"{prefix.value}_")
