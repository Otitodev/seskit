"""SESKit Python SDK — a thin client over the HTTP API (§13).

    from seskit import SesKit

    client = SesKit(api_key="sk_live_...", base_url="https://seskit.example.com")

    sent = client.emails.send(
        from_="hello@example.com",
        to=["user@example.com"],
        subject="Welcome",
        html="<h1>Welcome!</h1>",
    )
    client.emails.get(sent.id)
    client.emails.list(status="failed")

**Business logic lives in the API and is never duplicated here.** That is a
constraint from §13, and it has a consequence worth stating plainly: this
client can never do anything a `curl` command cannot. What it adds is typing,
retries that are safe because a send carries an idempotency key, and errors
that are classes rather than strings.

So reaching for it is a convenience and never a requirement. If your language
is not Python, or you would rather not add a dependency, an HTTP request is a
first-class way to use SESKit.
"""

from seskit._client import Emails, SesKit
from seskit._errors import (
    AttachmentTooLarge,
    AuthenticationFailed,
    AuthorizationFailed,
    DomainNotVerified,
    EmailRejected,
    InternalError,
    InvalidRecipient,
    InvalidRequest,
    NotFound,
    ProviderError,
    RateLimitExceeded,
    SendingLimitExceeded,
    SESKitConnectionError,
    SESKitError,
    SuppressedRecipient,
)
from seskit._models import Accepted, Email, EmailPage
from seskit.resources import Attachment

__version__ = "0.1.0"

__all__ = [
    "Accepted",
    "Attachment",
    "AttachmentTooLarge",
    "AuthenticationFailed",
    "AuthorizationFailed",
    "DomainNotVerified",
    "Email",
    "EmailPage",
    "EmailRejected",
    "Emails",
    "InternalError",
    "InvalidRecipient",
    "InvalidRequest",
    "NotFound",
    "ProviderError",
    "RateLimitExceeded",
    "SESKitConnectionError",
    "SESKitError",
    "SendingLimitExceeded",
    "SesKit",
    "SuppressedRecipient",
    "__version__",
]
