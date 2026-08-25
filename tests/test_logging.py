"""Log redaction (§22).

These are security tests, not cosmetic ones: §22 forbids API keys, AWS
credentials, and email bodies from reaching the logs at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from seskit_core.logging import REDACTED, redact_sensitive, request_id_var


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_sensitive(None, "info", event))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "secret",
        "SECRET_KEY",
        "api_key",
        "apiKey",
        "hashed_key",
        "authorization",
        "aws_secret_access_key",
        "aws_access_key_id",
        "session_token",
        "signature",
        "cookie",
        "credential",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    result = _redact({key: "super-secret-value"})

    assert result[key] == REDACTED


@pytest.mark.parametrize("key", ["html", "text", "html_body", "text_body", "body"])
def test_email_content_is_redacted(key: str) -> None:
    """Customer mail content stays out of logs by default (§6, §21)."""
    result = _redact({key: "<h1>Customer's private email</h1>"})

    assert result[key] == REDACTED


def test_nested_secrets_are_redacted() -> None:
    """A secret nested in a payload is just as leaked as a top-level one."""
    result = _redact({"payload": {"user": "alice", "api_key": "sk_live_123"}})

    assert result["payload"] == {"user": "alice", "api_key": REDACTED}


def test_secrets_inside_lists_are_redacted() -> None:
    result = _redact({"items": [{"password": "hunter2"}, {"name": "safe"}]})

    assert result["items"] == [{"password": REDACTED}, {"name": "safe"}]


def test_non_sensitive_values_survive() -> None:
    """Redaction must not gut the logs - the useful fields have to remain."""
    event = {
        "event": "email_sent",
        "email_id": "email_01J",
        "project_id": "proj_1",
        "status_code": 200,
        "to": "user@example.com",
    }

    assert _redact(dict(event)) == event


def test_matching_is_case_insensitive_and_partial() -> None:
    result = _redact({"AWS_Secret_Access_Key": "x", "user_password_hash": "y"})

    assert result["AWS_Secret_Access_Key"] == REDACTED
    assert result["user_password_hash"] == REDACTED


def test_request_id_context_var_defaults_to_none() -> None:
    assert request_id_var.get() is None
