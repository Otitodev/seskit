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

#: Where the HTTPS receiver listens. In core rather than in the API package
#: because provisioning subscribes SNS to this URL and the route serves it:
#: two copies of the string would mean a subscription pointing at a path
#: that answers 404, which looks exactly like events not working.
EVENT_HTTPS_PATH = "/v1/events/ses"

# Placeholder used in .env.example. Refusing to boot on this value is what stops
# it reaching a real deployment.
INSECURE_PLACEHOLDER = "changeme"


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EventIngestion(StrEnum):
    """How delivery events reach this instance (§15).

    SQS is the default because it works everywhere §9 says SESKit has to
    run: no inbound port, no public hostname, no certificate. AWS cannot
    POST to a laptop, and a self-hosted tool that only works on a public
    address is not self-hosted in the sense that matters.
    """

    SQS = "sqs"
    HTTPS = "https"
    BOTH = "both"


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

    # -- Delivery events (§15) -----------------------------------------------

    #: Names the SQS queue and SNS topic SESKit creates, so a shared AWS account
    #: can host more than one instance without them fighting over one queue.
    #: Changing it after setup orphans the previous resources - teardown removes
    #: what was recorded, not what the current setting would name.
    EVENT_RESOURCE_PREFIX: str = "seskit"

    #: The SES configuration set sends go through. Without one SES publishes no
    #: events at all, which is why it is recorded per message rather than
    #: assumed: a message sent before setup genuinely has no delivery history,
    #: and that is different from one whose events were lost.
    EVENT_CONFIGURATION_SET: str = "seskit"

    #: How events reach this instance. See EventIngestion.
    EVENT_INGESTION: EventIngestion = EventIngestion.SQS

    #: Where SNS can reach this instance, when the HTTPS receiver is in use -
    #: e.g. ``https://mail.example.com``. No default: guessing at a public URL
    #: and subscribing SNS to it would create a subscription that silently never
    #: confirms, which looks exactly like events not working.
    PUBLIC_BASE_URL: str | None = None

    #: How long a poll waits on an empty queue. SQS caps this at twenty
    #: seconds. Long polling rather than a busy loop: short polling forces a
    #: choice between latency and a billed request every few hundred
    #: milliseconds, forever.
    EVENT_POLL_WAIT_SECONDS: int = 20

    #: How many batches one scheduled pass will drain before giving up its
    #: turn. A bounded pass is what stops a backlog from monopolising the
    #: worker and starving sends, which are the thing users actually notice.
    EVENT_POLL_MAX_BATCHES: int = 10

    #: How long a consumer has to record an event before the message returns.
    #: Must comfortably exceed one ingest, or a slow database turns into
    #: duplicate processing - harmless, because of the unique constraint, but
    #: only because it is there.
    EVENT_VISIBILITY_TIMEOUT_SECONDS: int = 60

    # -- Identity verification (§10) -----------------------------------------

    #: How long before an unverified identity is re-checked against SES. DNS can
    #: take hours to propagate, so checking more often mostly spends API quota
    #: to be told the same thing.
    IDENTITY_RECHECK_UNVERIFIED_SECONDS: int = 6 * 60 * 60

    #: And a verified one. Far less frequent, but not never: this is what
    #: catches a DKIM record deleted months after setup, which otherwise looks
    #: healthy right up until a send fails.
    IDENTITY_RECHECK_VERIFIED_SECONDS: int = 30 * 24 * 60 * 60

    #: Floor on how often a user may force a re-check from the dashboard, so
    #: holding the button cannot spend the project's SES quota.
    IDENTITY_REFRESH_INTERVAL_SECONDS: int = 60

    # -- Sending (§11, §14) ---------------------------------------------------

    #: SES rejects a raw message over 10 MB, so we refuse it first and say why
    #: (§19 attachment_too_large) rather than passing a provider error through.
    #: Checked against the *assembled* message: base64 inflates content by about
    #: a third, so a 9 MB attachment is an over-limit message.
    EMAIL_MAX_MESSAGE_BYTES: int = 10 * 1024 * 1024

    #: How many times the worker will retry a send that failed for a reason
    #: worth retrying. Terminal rejections are not retried at all.
    EMAIL_SEND_MAX_ATTEMPTS: int = 3

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
    def event_https_endpoint(self) -> str | None:
        """The URL to subscribe SNS to, or None if we cannot know it.

        None rather than a guess: subscribing SNS to a URL that is not really
        this instance creates a subscription that never confirms, and a
        never-confirmed subscription looks exactly like events not working.
        """
        if not self.receives_https or not self.PUBLIC_BASE_URL:
            return None
        return f"{self.PUBLIC_BASE_URL.rstrip('/')}{EVENT_HTTPS_PATH}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def polls_sqs(self) -> bool:
        """Whether the worker should poll a queue for events."""
        return self.EVENT_INGESTION in (EventIngestion.SQS, EventIngestion.BOTH)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def receives_https(self) -> bool:
        """Whether the HTTPS receiver should accept SNS notifications.

        Off by default. An endpoint that exists but is not subscribed to is
        only an attack surface, and this one is reachable without
        authentication by design - SNS cannot present a credential.
        """
        return self.EVENT_INGESTION in (EventIngestion.HTTPS, EventIngestion.BOTH)

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
