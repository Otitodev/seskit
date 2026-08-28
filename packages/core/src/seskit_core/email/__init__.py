"""Message assembly.

Provider-agnostic: what goes out over SMTP and what SES is handed as raw
content are the same bytes, built once.
"""

from seskit_core.email.message import (
    RESERVED_HEADERS,
    assert_within_size,
    build_message,
    envelope_recipients,
    message_bytes,
)

__all__ = [
    "RESERVED_HEADERS",
    "assert_within_size",
    "build_message",
    "envelope_recipients",
    "message_bytes",
]
