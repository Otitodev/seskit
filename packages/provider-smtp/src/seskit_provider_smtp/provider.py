"""The SMTP provider (§25, §26).

Built before the SES send path on purpose. §31 says it "unblocks testing every
later phase without AWS", and `docs/design/prior-art.md` records the sharper reason: a
new AWS account cannot mail an arbitrary recipient for about 24 hours, so if the
first successful send depended on AWS, nobody would reach one on their first
afternoon. Pointed at Mailpit, `POST /v1/emails` works the moment
`docker compose up` finishes.

This is not a production alternative to SES - §3 keeps other production
providers a non-goal - but it implements the same interface, which is what makes
the provider abstraction worth having this early rather than in the abstract.

`aiosmtplib` rather than the stdlib `smtplib`: this runs on the worker's event
loop, and a blocking socket write would stall every other job. The SES adapter
reaches for `asyncio.to_thread` only because boto3 leaves it no choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage

import aiosmtplib
from seskit_core.email import build_message, envelope_recipients
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.providers.types import (
    AccountStatus,
    CredentialMode,
    IdentityStatus,
    IdentityType,
    OutboundEmail,
    SendingQuota,
    SentMessage,
    VerificationStatus,
)

logger = get_logger(__name__)

#: SMTP has no notion of a sending quota. Reporting zero would read as "you may
#: send nothing"; reporting a large number is honest about there being no cap
#: this side of the server's own limits.
UNLIMITED = float("inf")


@dataclass(frozen=True, slots=True)
class SMTPSettings:
    """What the provider needs to reach a server.

    A small value object rather than the whole `Settings`, so the provider can
    be constructed in a test without building application configuration.
    """

    host: str
    port: int = 1025
    use_tls: bool = False
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0


class SMTPProvider:
    """Sends through a plain SMTP server - Mailpit in local development."""

    def __init__(self, settings: SMTPSettings) -> None:
        self.settings = settings

    # -------------------------------------------------------------- account ---

    async def verify_account(self) -> AccountStatus:
        """There is no account to verify.

        Answered rather than raised so the dashboard and the send path can treat
        every provider alike. Not sandboxed: the sandbox is an SES concept, and
        claiming one here would put a warning on screen about a limit that does
        not exist.
        """
        return AccountStatus(
            account_id="local",
            region="local",
            sandbox=False,
            sending_enabled=True,
            enforcement_status="HEALTHY",
            quota=SendingQuota(
                max_24_hour_send=UNLIMITED, max_send_rate=UNLIMITED, sent_last_24_hours=0.0
            ),
            credential_mode=CredentialMode.UNKNOWN,
        )

    async def get_sending_quota(self) -> SendingQuota:
        return (await self.verify_account()).quota

    # ------------------------------------------------------------ identities ---

    async def create_identity(self, value: str, identity_type: IdentityType) -> IdentityStatus:
        """Everything is already verified.

        A local SMTP server will relay whatever it is given, so refusing here
        would invent a restriction the transport does not have - and would stop
        a developer sending before they have an AWS account, which is the whole
        point of this provider.
        """
        return await self.get_identity_status(value)

    async def get_identity_status(self, value: str) -> IdentityStatus:
        is_address = "@" in value
        return IdentityStatus(
            value=value,
            identity_type=IdentityType.EMAIL_ADDRESS if is_address else IdentityType.DOMAIN,
            verification_status=VerificationStatus.SUCCESS,
        )

    async def delete_identity(self, value: str) -> None:
        return None

    # ----------------------------------------------------------------- send ---

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        """Hand the assembled message to the SMTP server.

        Recipients come from :func:`envelope_recipients` rather than from the
        message headers, so blind copies are delivered without appearing in it.
        """
        mime = build_message(message)
        recipients = envelope_recipients(message)

        try:
            _, response = await aiosmtplib.send(
                mime,
                recipients=recipients,
                hostname=self.settings.host,
                port=self.settings.port,
                use_tls=self.settings.use_tls,
                username=self.settings.username,
                password=self.settings.password,
                timeout=self.settings.timeout,
            )
        except aiosmtplib.SMTPRecipientsRefused as exc:
            raise APIError(
                ErrorType.INVALID_RECIPIENT,
                "The SMTP server refused every recipient.",
            ) from exc
        except aiosmtplib.SMTPResponseException as exc:
            # The server answered, and said no. Terminal for a 5xx, worth
            # retrying for a 4xx - the send service decides which on the code.
            raise APIError(
                ErrorType.EMAIL_REJECTED,
                f"The SMTP server rejected the message ({exc.code}).",
            ) from exc
        except (aiosmtplib.SMTPException, OSError) as exc:
            # Could not talk to it at all. Always worth another attempt.
            logger.warning("smtp_unreachable", host=self.settings.host, exc_info=True)
            raise APIError(
                ErrorType.PROVIDER_ERROR,
                "Could not reach the SMTP server.",
            ) from exc

        return SentMessage(provider_message_id=_message_id(mime, response))


def _message_id(mime: EmailMessage, response: str) -> str:
    """Something to correlate on later.

    SMTP has no equivalent of an SES message id. Some servers echo one in the
    final response, so that is preferred when present; otherwise the Message-ID
    we set during assembly is used, which is better than nothing precisely
    because we chose it - a server-generated one is invisible to us.
    """
    for token in str(response).split():
        if token.startswith("<") and token.endswith(">"):
            return token.strip("<>")

    header = mime["Message-ID"]
    return str(header).strip("<>") if header else ""
