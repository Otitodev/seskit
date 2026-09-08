"""Reading the access key a project sends with (§31 Phase 14).

Its own module because both `aws.py` and `events.py` need it and `aws.py`
already imports `events.py` - putting it in either would be a cycle. Small
enough that a third home is cheaper than the alternative.
"""

from __future__ import annotations

from seskit_core.errors import APIError, ErrorType
from seskit_core.models import AWSConnection
from seskit_core.providers import AWSCredentials
from seskit_core.security.aws_credentials import (
    CredentialsUnreadable,
    decrypt_secret_access_key,
)


def stored_credentials(connection: AWSConnection, *, secret_key: str) -> AWSCredentials:
    """The access key this connection sends with.

    The only place a stored secret is decrypted. Eight callers need
    credentials, and eight copies of the decrypt call would be eight chances to
    log the result, hold it somewhere, or forget to translate the failure.

    Raises ``APIError`` rather than letting ``CredentialsUnreadable`` escape,
    because every caller is answering a request and the honest answer is the
    same one: this project must be connected again. That happens when
    SECRET_KEY has changed since the key was stored.
    """
    if not connection.has_credentials:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "This project has no AWS access key. Connect it on the AWS page.",
        )

    try:
        secret = decrypt_secret_access_key(
            connection.aws_secret_access_key_encrypted or "", secret_key=secret_key
        )
    except CredentialsUnreadable as error:
        raise APIError(ErrorType.INVALID_REQUEST, str(error)) from error

    return AWSCredentials(
        access_key_id=connection.aws_access_key_id or "",
        secret_access_key=secret,
    )
