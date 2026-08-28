"""Translating SES identity responses into core's vocabulary.

Kept beside the adapter rather than inside it because the mapping is fiddly in
its own right: SES reports verification through two different fields depending
on which call you made, uses upper-case status names, and has a third identity
type that does not concern us.
"""

from __future__ import annotations

from typing import Any

from seskit_core.providers.types import (
    IdentityStatus,
    IdentityType,
    VerificationStatus,
)

#: SES status strings, upper-cased, map onto our enum by lower-casing. Written
#: as an explicit table anyway: an unrecognised value must land somewhere
#: deliberate rather than crash a page, and PENDING is the honest default -
#: "we asked, we do not have an answer yet".
_STATUS_BY_NAME: dict[str, VerificationStatus] = {
    "PENDING": VerificationStatus.PENDING,
    "SUCCESS": VerificationStatus.SUCCESS,
    "FAILED": VerificationStatus.FAILED,
    "TEMPORARY_FAILURE": VerificationStatus.TEMPORARY_FAILURE,
    "NOT_STARTED": VerificationStatus.NOT_STARTED,
}

#: SES also reports MANAGED_DOMAIN, which only occurs for identities SES itself
#: manages. Nothing here creates one, and if we ever meet one it behaves like a
#: domain.
_TYPE_BY_NAME: dict[str, IdentityType] = {
    "DOMAIN": IdentityType.DOMAIN,
    "MANAGED_DOMAIN": IdentityType.DOMAIN,
    "EMAIL_ADDRESS": IdentityType.EMAIL_ADDRESS,
}


def parse_status(raw: str | None) -> VerificationStatus:
    if not raw:
        return VerificationStatus.PENDING
    return _STATUS_BY_NAME.get(raw.upper(), VerificationStatus.PENDING)


def parse_identity_type(raw: str | None, fallback: IdentityType) -> IdentityType:
    """SES tells us what it thinks the identity is; trust it over the caller.

    A user who types a domain into the address field should end up with what SES
    actually created, not with what the form assumed.
    """
    if not raw:
        return fallback
    return _TYPE_BY_NAME.get(raw.upper(), fallback)


def to_identity_status(
    value: str,
    response: dict[str, Any] | Any,
    *,
    fallback_type: IdentityType,
) -> IdentityStatus:
    """Build an ``IdentityStatus`` from a Create or Get response.

    The two responses differ in one important way: only ``GetEmailIdentity``
    carries a ``VerificationStatus`` field. ``CreateEmailIdentity`` reports just
    the boolean ``VerifiedForSendingStatus``, which for a brand new identity is
    always false - so reading it as a status would render every freshly added
    domain as FAILED rather than PENDING.
    """
    identity_type = parse_identity_type(response.get("IdentityType"), fallback_type)

    if "VerificationStatus" in response:
        verification = parse_status(response.get("VerificationStatus"))
    elif response.get("VerifiedForSendingStatus"):
        # A create that came back already verified means the identity existed.
        verification = VerificationStatus.SUCCESS
    else:
        verification = VerificationStatus.PENDING

    dkim: VerificationStatus | None = None
    tokens: list[str] = []
    mail_from: VerificationStatus | None = None

    # DKIM and MAIL FROM are inapplicable to an email address. Leaving them None
    # is the whole reason those columns are nullable - "pending" would promise
    # something that can never resolve.
    if identity_type is IdentityType.DOMAIN:
        dkim_attributes = response.get("DkimAttributes") or {}
        dkim = parse_status(dkim_attributes.get("Status"))
        tokens = list(dkim_attributes.get("Tokens") or [])

        mail_from_attributes = response.get("MailFromAttributes") or {}
        raw_mail_from = mail_from_attributes.get("MailFromDomainStatus")
        # Absent means no custom MAIL FROM is configured, which is the default
        # and not a failure. NOT_STARTED says that; PENDING would imply we are
        # waiting on something.
        mail_from = parse_status(raw_mail_from) if raw_mail_from else VerificationStatus.NOT_STARTED

    return IdentityStatus(
        value=value,
        identity_type=identity_type,
        verification_status=verification,
        dkim_status=dkim,
        mail_from_status=mail_from,
        dkim_tokens=tokens,
    )
