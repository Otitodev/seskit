"""What a `/v1` failure becomes in Python (§19, §31 Phase 12).

Every SESKit failure arrives as ``{"error": {"type": ..., "message": ...}}``.
The type is stable and domain-shaped; the message is written for a human and
may be reworded without warning. So the type becomes a class and the message
becomes text on it — code branches on the class, and nobody is tempted to match
on prose.

**These names are duplicated from the API on purpose.** The SDK is a standalone
distribution whose only dependency is httpx; importing `seskit_core` to reach
`ErrorType` would drag the server, SQLAlchemy and boto3 into every application
that wants to send an email. The duplication is kept honest by a test that
imports both and fails if the API can raise a type the SDK has no class for.
"""

from __future__ import annotations

from typing import Any


class SESKitError(Exception):
    """Anything the API refused.

    Catch this to catch every refusal. Catch a subclass to act on one.
    """

    #: The `error.type` this class stands for. Empty on the base class, which
    #: is raised only when the API returns a type this version has never heard
    #: of — a client from before an error type existed should still raise
    #: something a caller can catch, rather than a KeyError from the SDK.
    error_type: str = ""

    def __init__(self, message: str, *, status_code: int, error_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.type = error_type if error_type is not None else self.error_type

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.status_code} {self.type}>"


class InvalidRequest(SESKitError):
    """The request cannot be acted on, or its body failed validation."""

    error_type = "invalid_request"


class AuthenticationFailed(SESKitError):
    """Missing, malformed, unknown or revoked API key.

    One class for all four, as the API gives one answer for all four: telling
    them apart would say which guesses were closer.
    """

    error_type = "authentication_failed"


class AuthorizationFailed(SESKitError):
    """Authenticated, but not for this."""

    error_type = "authorization_failed"


class NotFound(SESKitError):
    """No such resource in this project.

    Also what an id belonging to somebody else's project returns, which is why
    it is not `AuthorizationFailed` — a 403 would confirm the id exists.
    """

    error_type = "not_found"


class DomainNotVerified(SESKitError):
    """The `from` address is not covered by a verified identity."""

    error_type = "domain_not_verified"


class InvalidRecipient(SESKitError):
    """An address in `to`, `cc` or `bcc` is not an email address."""

    error_type = "invalid_recipient"


class SuppressedRecipient(SESKitError):
    """An address is on the project's suppression list.

    The one refusal in this module a person can clear: the list is the
    project's own, and the dashboard can take an address off it.
    """

    error_type = "suppressed_recipient"


class EmailRejected(SESKitError):
    """SES refused the message itself."""

    error_type = "email_rejected"


class AttachmentTooLarge(SESKitError):
    """The assembled message is over the limit.

    Assembled, not the sum of the files: base64 inflates content by about a
    third, and it is the assembled size the provider rejects.
    """

    error_type = "attachment_too_large"


class RateLimitExceeded(SESKitError):
    """Over SESKit's own per-project limit. Retried automatically."""

    error_type = "rate_limit_exceeded"


class SendingLimitExceeded(SESKitError):
    """Over the SES account's quota, which SESKit cannot raise."""

    error_type = "sending_limit_exceeded"


class ProviderError(SESKitError):
    """SES refused or failed. The message is normalised, never raw AWS text."""

    error_type = "provider_error"


class InternalError(SESKitError):
    """Unexpected, on the server. Worth reporting."""

    error_type = "internal_error"


class SESKitConnectionError(SESKitError):
    """The request never got an answer.

    Distinct from every class above, which are answers. A send that failed this
    way may or may not have been accepted, which is what an `idempotency_key`
    is for.
    """

    error_type = "connection_error"


#: Every class above, by the type it stands for. Built from the classes rather
#: than written out again, so adding a class is the only step.
ERROR_CLASSES: dict[str, type[SESKitError]] = {
    subclass.error_type: subclass
    for subclass in SESKitError.__subclasses__()
    if subclass.error_type
}


def error_from(payload: Any, *, status_code: int) -> SESKitError:
    """Turn one error response into the exception to raise.

    Tolerant of a body that is not the documented envelope: a proxy returning
    its own HTML 502, or a URL that is not a SESKit instance at all. Those must
    still raise something a caller can catch, and must not raise a
    `TypeError` from inside the SDK, which tells them nothing.
    """
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return SESKitError(
            f"The server returned {status_code} and a body this client did not understand.",
            status_code=status_code,
        )

    error_type = str(error.get("type", ""))
    message = str(error.get("message", "")) or f"The request failed with {status_code}."
    cls = ERROR_CLASSES.get(error_type, SESKitError)
    return cls(message, status_code=status_code, error_type=error_type)
