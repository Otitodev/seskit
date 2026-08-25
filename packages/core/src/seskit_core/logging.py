"""Structured JSON logging (§21) with secret redaction (§22).

Redaction is a processor rather than a convention because §22 makes it a hard
requirement: API keys, AWS credentials, and email bodies must never reach the
logs. A convention gets forgotten at 2am; a processor does not.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, Processor

#: Set by the API's request-ID middleware; read by `add_request_context` so every
#: log line emitted while handling a request carries its ID without being passed
#: down through every call site.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REDACTED = "[redacted]"

#: Keys whose values are never safe to log. Matched case-insensitively against
#: any part of the key, so `aws_secret_access_key` and `SecretKey` both hit.
SENSITIVE_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "apikey",
        "hashed_key",
        "credential",
        "access_key",
        "session_token",
        "signature",
        "cookie",
    }
)

#: Email content, kept out of logs by default per §6 and §21. Not "secret"
#: exactly, but logging customer mail bodies is a privacy problem.
CONTENT_KEYS: frozenset[str] = frozenset({"html", "text", "html_body", "text_body", "body"})


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in CONTENT_KEYS:
        return True
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_sensitive(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Replace the value of any sensitive key, at any nesting depth."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: REDACTED if _is_sensitive(str(k)) else scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, tuple):
            return tuple(scrub(v) for v in value)
        return value

    return {
        key: REDACTED if _is_sensitive(str(key)) else scrub(value)
        for key, value in event_dict.items()
    }


def add_request_context(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Attach the ambient request ID, when one is set."""
    request_id = request_id_var.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def configure_logging(*, log_level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and route the stdlib logging module through it.

    Both structlog calls and stdlib calls (uvicorn, SQLAlchemy) end up in one
    handler with one renderer, so the output is uniformly structured rather than
    half JSON and half free text.

    ``json_output=False`` gives a human-readable console renderer, which is
    friendlier when tailing logs locally.
    """
    # Applied to structlog and stdlib records alike, so a uvicorn line carries
    # the same request_id and redaction as an application line.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        add_request_context,
        redact_sensitive,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Hands the event dict to the stdlib handler's formatter, which does
            # the actual rendering. Must be last.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # add_logger_name reads `logger.name`, so the factory must produce
        # stdlib loggers - a PrintLogger has no name and raises.
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Records from stdlib loggers have not been through the structlog
            # pipeline, so they get the shared processors here instead.
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    # uvicorn and arq both install handlers of their own. Left in place, every
    # line from them is emitted twice: once in their format, once in ours.
    # Clearing the handlers and letting the records propagate to root leaves a
    # single, uniformly-formatted stream.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq", "arq.worker"):
        stdlib_logger = logging.getLogger(noisy)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
