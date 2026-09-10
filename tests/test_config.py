"""Configuration loading and its guardrails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from seskit_core.config import Environment, Settings


def _base_env() -> dict[str, str | None]:
    return {
        "SECRET_KEY": "a-real-secret",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
        # Cleared explicitly. _env_file=None keeps a local .env out, but
        # pydantic-settings still reads os.environ - and conftest sets these so
        # the send tests can reach Mailpit on the host. Without this the SMTP
        # cases below would silently test the ambient environment instead of
        # their own arguments.
        "SMTP_HOST": None,
        "EMAILS_FROM_EMAIL": None,
    }


def _settings(**overrides: str | None) -> Settings:
    """Build Settings from explicit values only.

    Two leaks to keep out, not one. ``_env_file=None`` stops a developer's local
    .env being read - without it these pass in CI and fail on a machine that has
    run the quickstart. ``_base_env`` then clears the variables the test session
    itself sets, since init arguments beat os.environ but only for keys that are
    actually passed.
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


# --------------------------------------------------- blank means unset (§32) ---

# Found by a deployment, not by this suite. Five variables were declared with no
# value on a hosting platform, the container went into a restart loop, and the
# message was five validation errors about settings nobody had knowingly set:
#
#   LOG_LEVEL   Input should be 'DEBUG', 'INFO', 'WARNING' or 'ERROR'
#               [type=literal_error, input_value='', input_type=str]
#
# Most platforms represent "declared, not set" as an empty string rather than by
# leaving the variable out, so this is the normal case for a self-hosted product
# rather than an unusual one.


def _optional_fields() -> list[str]:
    return sorted(name for name, f in Settings.model_fields.items() if not f.is_required())


@pytest.mark.parametrize("name", _optional_fields())
def test_a_blank_value_falls_back_to_the_default(name: str) -> None:
    """Every optional setting, so a new one is covered the day it is added.

    28 of these used to raise - every integer, boolean, enum and literal among
    them. Which ones a deployment hit depended only on which its platform
    happened to declare, so fixing the five that broke would have left the next
    target to find another five.
    """
    default = Settings.model_fields[name].default

    settings = _settings(**{name: ""})

    assert getattr(settings, name) == default, f"{name} did not fall back to its default"


@pytest.mark.parametrize("name", _optional_fields())
def test_whitespace_is_blank_too(name: str) -> None:
    """A value of " " is a variable somebody left empty, not a setting of one
    space. No integer, enum or boolean could accept it anyway.
    """
    assert getattr(_settings(**{name: "   "}), name) == Settings.model_fields[name].default


def test_a_blank_value_is_not_silently_kept_for_a_string_setting() -> None:
    """The quiet half, and the more dangerous one.

    These eleven never raised - they took the empty string as the value. An
    empty region reaches boto3; an empty configuration set means SES publishes
    no events at all; an empty cookie name breaks every session. All of them
    start cleanly and fail somewhere else entirely.
    """
    settings = _settings(
        AWS_DEFAULT_REGION="",
        EVENT_CONFIGURATION_SET="",
        SESSION_COOKIE_NAME="",
        PROJECT_NAME="",
    )

    assert settings.AWS_DEFAULT_REGION == "us-east-1"
    assert settings.EVENT_CONFIGURATION_SET == "seskit"
    assert settings.SESSION_COOKIE_NAME == "seskit_session"
    assert settings.PROJECT_NAME == "SESKit"


@pytest.mark.parametrize("name", ["SECRET_KEY", "DATABASE_URL", "REDIS_URL"])
def test_a_blank_required_setting_still_refuses_to_boot(name: str) -> None:
    """These three have no default, and there is nothing to fall back to.

    Defaulting one away is how an instance ends up signing sessions with a key
    nobody chose, so a blank here has to stay as loud as an absent one.
    """
    with pytest.raises(ValidationError):
        _settings(**{name: ""})


def test_the_deployment_that_found_this_would_now_start() -> None:
    """The exact five, in the state the platform left them."""
    settings = _settings(
        LOG_LEVEL="",
        AWS_STATUS_CACHE_TTL_SECONDS="",
        EVENT_INGESTION="",
        SMTP_PORT="",
        SMTP_TLS="",
    )

    assert settings.LOG_LEVEL == "INFO"
    assert settings.AWS_STATUS_CACHE_TTL_SECONDS == 300
    assert settings.EVENT_INGESTION.value == "sqs"
    assert settings.SMTP_PORT == 1025
    assert settings.SMTP_TLS is False
