"""The public API's error vocabulary (§19).

Every ``/v1`` failure leaves the building in one shape::

    {"error": {"type": "domain_not_verified", "message": "..."}}

Defined here, in core rather than in the API app, because the SDK (Phase 10)
and any future CLI (§24) need the same vocabulary and must not each invent
their own spelling of it.

The full type list from §19 is declared now even though this phase can only
raise a few of them. A later phase adding ``domain_not_verified`` should find
the name already chosen rather than coin ``domain_unverified`` beside it.

Raw provider errors never reach a customer: §19 is explicit that boto3 and AWS
exceptions must be normalised into these types, which is Phase 4's job.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorType(StrEnum):
    """The `error.type` values a customer may see."""

    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    DOMAIN_NOT_VERIFIED = "domain_not_verified"
    SENDING_LIMIT_EXCEEDED = "sending_limit_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    PROVIDER_ERROR = "provider_error"
    INVALID_RECIPIENT = "invalid_recipient"
    ATTACHMENT_TOO_LARGE = "attachment_too_large"
    EMAIL_REJECTED = "email_rejected"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"


#: The HTTP status that goes with each type, so a route never has to pair them
#: by hand and two endpoints cannot answer the same failure differently.
STATUS_FOR_TYPE: dict[ErrorType, int] = {
    ErrorType.INVALID_REQUEST: 400,
    ErrorType.AUTHENTICATION_FAILED: 401,
    ErrorType.AUTHORIZATION_FAILED: 403,
    ErrorType.NOT_FOUND: 404,
    ErrorType.DOMAIN_NOT_VERIFIED: 422,
    ErrorType.INVALID_RECIPIENT: 422,
    ErrorType.ATTACHMENT_TOO_LARGE: 413,
    ErrorType.EMAIL_REJECTED: 422,
    ErrorType.RATE_LIMIT_EXCEEDED: 429,
    ErrorType.SENDING_LIMIT_EXCEEDED: 429,
    ErrorType.PROVIDER_ERROR: 502,
    ErrorType.INTERNAL_ERROR: 500,
}


class APIError(Exception):
    """A failure meant for a customer's application to read.

    Carries its own status and optional headers, so a handler can render it
    without a table of special cases. Anything raised as an ``APIError`` is
    something we have decided to say out loud - an unexpected exception becomes
    a generic ``internal_error`` instead, so an implementation detail cannot
    leak through a traceback.
    """

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status_code = status_code or STATUS_FOR_TYPE[error_type]
        self.headers = headers or {}

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {"error": {"type": self.error_type.value, "message": self.message}}

    def __repr__(self) -> str:
        return f"<APIError {self.error_type.value} {self.status_code}>"


class AuthenticationFailed(APIError):
    """No usable API key on the request.

    One message for a missing key, a malformed key, an unknown key, and a
    revoked one. Distinguishing them would tell a caller which guesses were
    closer.
    """

    def __init__(self, message: str = "Invalid or missing API key.") -> None:
        super().__init__(ErrorType.AUTHENTICATION_FAILED, message)


class RateLimitExceeded(APIError):
    """The project has spent its allowance for this window."""

    def __init__(self, retry_after: int, message: str = "Too many requests.") -> None:
        super().__init__(
            ErrorType.RATE_LIMIT_EXCEEDED,
            message,
            headers={"Retry-After": str(retry_after)},
        )
