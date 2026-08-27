"""Application configuration.

All settings come from the environment (or a local ``.env``). Nothing here is
hard-coded per §32.6, and no secret ever gains a usable default - see
``_reject_insecure_defaults``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, PostgresDsn, RedisDsn, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder used in .env.example. Refusing to boot on this value is what stops
# it reaching a real deployment.
INSECURE_PLACEHOLDER = "changeme"


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- General ------------------------------------------------------------
    PROJECT_NAME: str = "SESKit"
    ENVIRONMENT: Environment = Environment.LOCAL
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    #: Signs sessions and, later, webhook payloads (§16).
    SECRET_KEY: str = Field(min_length=1)

    # -- Persistence --------------------------------------------------------
    DATABASE_URL: PostgresDsn
    REDIS_URL: RedisDsn

    # -- Dashboard authentication -------------------------------------------

    #: Registration is always permitted while no account exists, so a fresh
    #: install can be claimed by whoever sets it up. It closes immediately
    #: afterwards unless this is turned on: a self-hosted instance is often
    #: reachable before anyone is watching it, and an open signup form on a
    #: public URL means a stranger can take it over.
    ALLOW_SIGNUP: bool = False

    SESSION_COOKIE_NAME: str = "seskit_session"

    #: Idle timeout, not absolute - reading a session refreshes it, so an active
    #: user is never signed out mid-task.
    SESSION_TTL_DAYS: int = 14

    #: Failed logins per email and IP before the door closes for a while.
    LOGIN_MAX_ATTEMPTS: int = 10
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 900

    # -- Public API (§7, §20) ------------------------------------------------

    #: How long a verified key stays resolved in Redis. Short, and only a
    #: backstop: revocation deletes the entry outright rather than waiting for
    #: this to elapse.
    API_KEY_CACHE_TTL_SECONDS: int = 60

    #: ``last_used_at`` is written at most once per key per interval. Writing on
    #: every request would add a database write to every API call, for a column
    #: read a few times a day.
    API_KEY_LAST_USED_INTERVAL_SECONDS: int = 60

    #: Per project, not per key, so a second key cannot buy more quota. §20's
    #: suggested starting point; configurable because instances differ.
    API_RATE_LIMIT_PER_MINUTE: int = 100
    API_RATE_LIMIT_WINDOW_SECONDS: int = 60

    # -- AWS (§8, §9) --------------------------------------------------------
    #
    # Deliberately no AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY here. §9 says
    # credentials are resolved the standard boto3 way - instance role,
    # environment, credential file, workload identity - and are never handled by
    # SESKit as data. boto3 reads the conventional variables itself; naming them
    # here would invite them into logs and into this object's repr.

    #: Pre-selects the region in the connect form. Not a credential, and not
    #: authoritative - the region actually in use is stored per connection.
    AWS_DEFAULT_REGION: str = "us-east-1"

    #: How long a rendered AWS connection stays cached. Longer than the API key
    #: cache because sandbox state and quota change a handful of times in an
    #: account's life, and the page has an explicit Refresh for the moment they
    #: do.
    AWS_STATUS_CACHE_TTL_SECONDS: int = 300

    # -- Local email provider (§25) -----------------------------------------
    # Points at Mailpit in local development. Production sending goes through
    # Amazon SES instead; see the provider-selection note on `smtp_configured`.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 1025
    SMTP_TLS: bool = False
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT is Environment.LOCAL

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_ttl_seconds(self) -> int:
        return self.SESSION_TTL_DAYS * 24 * 60 * 60

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_cookie_secure(self) -> bool:
        """Whether to set the Secure flag on the session cookie.

        Cannot be unconditional: a Secure cookie is not sent over plain HTTP, so
        forcing it on would break login at http://localhost, which is the
        documented way to run this (§25).
        """
        return not self.is_local

    @computed_field  # type: ignore[prop-decorator]
    @property
    def smtp_configured(self) -> bool:
        """Whether the local SMTP provider can send.

        Phase 6 uses this to pick a provider: a project with no verified AWS
        connection falls back to SMTP (Mailpit) so the product is usable
        immediately after ``docker compose up`` (§25).
        """
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """psycopg-style URL, for tools that cannot drive an async driver."""
        return str(self.DATABASE_URL).replace("postgresql+asyncpg://", "postgresql://", 1)

    @model_validator(mode="after")
    def _require_async_driver(self) -> Self:
        if not str(self.DATABASE_URL).startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the postgresql+asyncpg:// scheme - "
                "SESKit drives the database asynchronously."
            )
        return self

    @model_validator(mode="after")
    def _reject_insecure_defaults(self) -> Self:
        """Refuse to boot outside local with placeholder secrets still in place."""
        if self.ENVIRONMENT is Environment.LOCAL:
            return self
        if self.SECRET_KEY == INSECURE_PLACEHOLDER:
            raise ValueError(
                f"SECRET_KEY is still set to the placeholder value and "
                f"ENVIRONMENT is {self.ENVIRONMENT}. Generate a real secret before deploying."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    # Values come from the environment, not from arguments.
    return Settings()
