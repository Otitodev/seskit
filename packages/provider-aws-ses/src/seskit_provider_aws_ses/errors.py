"""Turning botocore exceptions into §19's vocabulary.

§19 is explicit: "Do not expose raw boto3/AWS exceptions to customers." That is
not only about tidiness. A botocore error string routinely carries the full ARN
of the calling principal, the role name, and the exact API action - useful in a
log, not something to render into a page or return to a caller's application.

So every call into AWS passes through :func:`normalise_boto_error`, and no route
or service above the adapter ever sees a ``ClientError``. Anything unrecognised
becomes ``provider_error`` with a generic message; the detail goes to the log.
"""

from __future__ import annotations

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger

logger = get_logger(__name__)

#: Message shown when boto3 finds no credentials at all. Names the chain,
#: because "no credentials" without saying where SESKit looked leaves a
#: self-hoster with nowhere to start.
NO_CREDENTIALS_MESSAGE = (
    "No AWS credentials found. SESKit resolves credentials the standard boto3 "
    "way: an IAM role on the instance or task, the AWS_ACCESS_KEY_ID and "
    "AWS_SECRET_ACCESS_KEY environment variables, or a shared credentials file."
)

#: botocore error codes that mean the identity is real but not permitted.
_AUTHORIZATION_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AuthorizationError",
    }
)

#: Codes that mean the credentials themselves are bad.
# Four different things AWS says about a credential, which used to come back
# as one sentence: "rejected - expired or incorrect". On a real host that sent
# a user to rotate a key that was fine, twice, when the secret field had been
# refilled by a password manager. Each code now says which half is wrong.

#: The access key ID is not one AWS knows. Mistyped, deleted, or not yet
#: propagated to the regional endpoint - a key made seconds ago can be this.
_UNKNOWN_KEY_CODES = frozenset(
    {"InvalidClientTokenId", "UnrecognizedClientException", "InvalidAccessKeyId"}
)

#: The access key ID is known and the secret does not go with it.
_WRONG_SECRET_CODES = frozenset({"SignatureDoesNotMatch"})

#: The access key ID could not even be read as one - a character AWS does not
#: allow, which is what a stray quote or a secret pasted into the ID field
#: looks like. Was unmapped, and so a 502 "did not complete".
_MALFORMED_KEY_CODES = frozenset({"IncompleteSignature", "InvalidSignatureException"})

#: A session token past its time. SESKit stores long-term keys, so this only
#: reaches a user who pasted temporary credentials from the console.
_EXPIRED_CODES = frozenset({"ExpiredToken", "ExpiredTokenException"})

_AUTHENTICATION_CODES = (
    _UNKNOWN_KEY_CODES | _WRONG_SECRET_CODES | _MALFORMED_KEY_CODES | _EXPIRED_CODES
)

UNKNOWN_KEY_MESSAGE = (
    "AWS does not recognise that access key ID. Check it was copied whole and is "
    "active in IAM; a key created moments ago can take a little while to be known."
)
MISMATCHED_KEY_MESSAGE = (
    "The secret access key does not match that access key ID. If your browser "
    "offered a saved password for the field, it was an older secret - clear the "
    "field and paste the new one."
)
MALFORMED_KEY_MESSAGE = (
    "That is not an access key ID as AWS reads it - check nothing extra was "
    "pasted, and that the ID and the secret are in the right fields."
)
EXPIRED_MESSAGE = (
    "Those AWS credentials have expired. SESKit needs a long-term access key, "
    "not temporary session credentials."
)

_NOT_FOUND_CODES = frozenset({"NotFoundException", "ResourceNotFoundException"})

#: The message itself was refused. Terminal - retrying sends the same thing
#: to the same place and gets the same answer.
_REJECTED_CODES = frozenset({"MessageRejected", "MailFromDomainNotVerifiedException"})

#: Sending is switched off for the account, usually after a reputation
#: review. Nothing the caller can fix by retrying.
_SENDING_PAUSED_CODES = frozenset(
    {"AccountSuspendedException", "SendingPausedException", "LimitExceededException"}
)

_THROTTLING_CODES = frozenset(
    {"Throttling", "ThrottlingException", "TooManyRequestsException", "RequestThrottled"}
)


def error_code(exc: ClientError) -> str:
    """The AWS error code, or "" when the response is not shaped as expected."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = error.get("Code")
    return str(code) if code else ""


def _credential_message(code: str) -> str:
    if code in _WRONG_SECRET_CODES:
        return MISMATCHED_KEY_MESSAGE
    if code in _MALFORMED_KEY_CODES:
        return MALFORMED_KEY_MESSAGE
    if code in _EXPIRED_CODES:
        return EXPIRED_MESSAGE
    return UNKNOWN_KEY_MESSAGE


def normalise_boto_error(exc: Exception, *, action: str) -> APIError:
    """Map a botocore exception onto an :class:`APIError`.

    ``action`` is the IAM action that was attempted, e.g. ``ses:GetAccount``.
    It appears in the authorization message because "access denied" without
    naming the action leaves the user guessing which permission to add - and
    §9 requires the minimum policy be documented, which is useless if the error
    does not say which part of it is missing.
    """
    if isinstance(exc, NoCredentialsError | PartialCredentialsError):
        return APIError(ErrorType.AUTHORIZATION_FAILED, NO_CREDENTIALS_MESSAGE)

    if isinstance(exc, ClientError):
        code = error_code(exc)

        if code in _AUTHORIZATION_CODES:
            return APIError(
                ErrorType.AUTHORIZATION_FAILED,
                f"The AWS identity is not permitted to call {action}. "
                f"Add {action} to its IAM policy and try again.",
            )
        if code in _AUTHENTICATION_CODES:
            return APIError(ErrorType.AUTHENTICATION_FAILED, _credential_message(code))
        if code in _NOT_FOUND_CODES:
            return APIError(ErrorType.NOT_FOUND, "The requested AWS resource was not found.")
        if code in _REJECTED_CODES:
            return APIError(
                ErrorType.EMAIL_REJECTED,
                "Amazon SES refused the message. Check the sender is verified and "
                "that the recipients are permitted - a sandboxed account may only "
                "send to verified addresses.",
            )
        if code in _SENDING_PAUSED_CODES:
            return APIError(
                ErrorType.SENDING_LIMIT_EXCEEDED,
                "Sending is currently not permitted for this AWS account.",
            )
        if code in _THROTTLING_CODES:
            return APIError(
                ErrorType.PROVIDER_ERROR,
                "Amazon SES is throttling requests. Try again in a moment.",
            )

        # Recognised as a ClientError but not as a code we handle. The code is
        # safe to log; the message is not safe to return.
        logger.warning("aws_unmapped_client_error", action=action, code=code)
        return APIError(ErrorType.PROVIDER_ERROR, _generic_message(action))

    if isinstance(exc, EndpointConnectionError | BotoConnectionError):
        return APIError(
            ErrorType.PROVIDER_ERROR,
            "Could not reach Amazon SES. Check the region and network access.",
        )

    if isinstance(exc, BotoCoreError):
        logger.warning("aws_botocore_error", action=action, error=type(exc).__name__)
        return APIError(ErrorType.PROVIDER_ERROR, _generic_message(action))

    # Not a botocore exception at all. Deliberately still normalised rather than
    # re-raised: this sits on a request path, and an unexpected type escaping
    # here would reach a customer as a traceback.
    logger.exception("aws_unexpected_error", action=action, error=type(exc).__name__)
    return APIError(ErrorType.PROVIDER_ERROR, _generic_message(action))


def _generic_message(action: str) -> str:
    return f"Amazon SES did not complete {action}. The details are in the server log."
