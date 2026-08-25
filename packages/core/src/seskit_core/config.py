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
