"""Configuration loading and its guardrails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from seskit_core.config import Environment, Settings


def _base_env() -> dict[str, str]:
    return {
        "SECRET_KEY": "a-real-secret",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }


def _settings(**overrides: str) -> Settings:
    """Build Settings from explicit values only.

    ``_env_file=None`` is essential: without it a developer's local .env leaks
    into these tests, so they would pass in CI (no .env) and fail on a machine
    that has run the quickstart.
    """
    return Settings(_env_file=None, **(_base_env() | overrides))  # type: ignore[arg-type]


def test_settings_load_from_environment() -> None:
    settings = _settings()

    assert settings.ENVIRONMENT is Environment.LOCAL
    assert settings.is_local is True


def test_sync_database_url_strips_the_async_driver() -> None:
    """Tools that cannot drive asyncpg still need a usable URL."""
    settings = _settings()

    assert settings.database_url_sync.startswith("postgresql://")
    assert "asyncpg" not in settings.database_url_sync


def test_non_async_database_url_is_rejected() -> None:
    """A sync driver would fail confusingly at first query; fail at boot instead."""
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        _settings(DATABASE_URL="postgresql://u:p@localhost:5432/db")


def test_placeholder_secret_is_allowed_locally() -> None:
    assert _settings(SECRET_KEY="changeme", ENVIRONMENT="local").SECRET_KEY == "changeme"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_placeholder_secret_is_refused_outside_local(environment: str) -> None:
    """The .env.example placeholder must never boot a deployed instance."""
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _settings(SECRET_KEY="changeme", ENVIRONMENT=environment)


def test_smtp_is_not_configured_without_a_from_address() -> None:
    """Both a host and a from-address are needed before SMTP can send."""
    assert _settings(SMTP_HOST="mailpit").smtp_configured is False


def test_smtp_is_not_configured_without_a_host() -> None:
    assert _settings(EMAILS_FROM_EMAIL="noreply@seskit.local").smtp_configured is False


def test_smtp_is_configured_with_host_and_from_address() -> None:
    settings = _settings(SMTP_HOST="mailpit", EMAILS_FROM_EMAIL="noreply@seskit.local")

    assert settings.smtp_configured is True
