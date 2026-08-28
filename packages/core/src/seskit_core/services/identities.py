"""Adding, re-checking and removing sending identities (§10).

Two things here are less obvious than they look.

**Removing a row is not removing an identity.** An SES identity belongs to the
AWS account and region, so two projects using ``example.com`` are pointing at
one thing. If either project could delete it, the other would silently stop
sending with nothing on screen to explain why. So deletion refcounts: the row
always goes, the SES identity only when the last reference does.

**Re-checking is on a schedule, not on every view.** Verification is
asynchronous and can take days, so a background job re-asks - rarely for
identities that are settled, more often for ones still waiting. The rare case
matters more than it seems: it is what catches a DKIM record deleted months
after setup, which otherwise looks healthy right up until a send fails.
"""

from __future__ import annotations

from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import Identity, utcnow
from seskit_core.providers import IdentityStatus, IdentityType
from seskit_core.services.aws import ProviderFactory, check_is_allowed

logger = get_logger(__name__)

#: Longest identity we will accept. A domain cannot exceed 253 octets and an
#: address is bounded well below the column width.
MAX_IDENTITY_LENGTH = 255

INVALID_IDENTITY_MESSAGE = (
    "Enter a domain like example.com, or an email address like you@example.com."
)


def classify(value: str) -> tuple[str, IdentityType]:
    """Normalise an identity and decide what kind it is.

    Lower-cased because DNS is case-insensitive and because a duplicate
    differing only in case would slip past the uniqueness constraint and become
    a second row pointing at one SES identity.

    Deliberately lenient: SES is the authority on whether an identity is
    acceptable, and it will say so far more precisely than a regular expression
    here. This only rejects input that is obviously neither thing, so the user
    gets a useful message instead of a provider error.
    """
    cleaned = value.strip().lower()

    if not cleaned or len(cleaned) > MAX_IDENTITY_LENGTH or " " in cleaned:
        raise APIError(ErrorType.INVALID_REQUEST, INVALID_IDENTITY_MESSAGE)

    if "@" in cleaned:
        local, _, domain = cleaned.partition("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise APIError(ErrorType.INVALID_REQUEST, INVALID_IDENTITY_MESSAGE)
        return cleaned, IdentityType.EMAIL_ADDRESS

    if "." not in cleaned or cleaned.startswith(".") or cleaned.endswith("."):
        raise APIError(ErrorType.INVALID_REQUEST, INVALID_IDENTITY_MESSAGE)

    return cleaned, IdentityType.DOMAIN


async def list_identities(session: AsyncSession, project_id: str) -> list[Identity]:
    """Every identity for a project, newest first."""
    result = await session.scalars(
        select(Identity).where(Identity.project_id == project_id).order_by(Identity.id.desc())
    )
    return list(result)


async def get_owned_identity(
    session: AsyncSession, *, identity_id: str, project_id: str
) -> Identity | None:
    """Return the identity only if it belongs to this project.

    Ownership is part of the query, matching ``get_owned_api_key``: there is no
    path that loads the row and then forgets to compare.
    """
    identity: Identity | None = await session.scalar(
        select(Identity).where(Identity.id == identity_id, Identity.project_id == project_id)
    )
    return identity


async def find_identity(
    session: AsyncSession, *, project_id: str, value: str, region: str
) -> Identity | None:
    identity: Identity | None = await session.scalar(
        select(Identity).where(
            Identity.project_id == project_id,
            Identity.value == value,
            Identity.region == region,
        )
    )
    return identity


async def count_other_references(session: AsyncSession, identity: Identity) -> int:
    """How many *other* projects hold this same SES identity.

    Keyed on (value, region) because that is what an SES identity is scoped to,
    and §9 gives the instance one AWS account at a time.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Identity)
        .where(
            Identity.value == identity.value,
            Identity.region == identity.region,
            Identity.id != identity.id,
        )
    )
    return int(count or 0)


async def add_identity(
    session: AsyncSession,
    provider_factory: ProviderFactory,
    *,
    project_id: str,
    value: str,
    region: str,
) -> Identity:
    """Ask SES to verify a domain or address, and record what it said.

    If the project already has this identity, its existing row is refreshed
    rather than a second one created - a user pressing Add twice should not get
    a constraint error.
    """
    cleaned, identity_type = classify(value)

    provider = provider_factory(region)
    # May adopt an existing identity: the adapter turns AlreadyExists into a
    # status read, so a domain another project verified arrives already SUCCESS.
    status = await provider.create_identity(cleaned, identity_type)

    identity = await find_identity(session, project_id=project_id, value=cleaned, region=region)
    if identity is None:
        identity = Identity(project_id=project_id, value=cleaned, region=region)
        session.add(identity)

    _apply(identity, status)
    await session.flush()

    logger.info(
        "identity_added",
        identity_id=identity.id,
        project_id=project_id,
        identity_type=identity.identity_type,
        verification_status=identity.verification_status,
    )
    return identity


async def refresh_identity(
    session: AsyncSession,
    redis: Redis,
    provider_factory: ProviderFactory,
    identity: Identity,
    *,
    interval_seconds: int,
) -> Identity:
    """Re-ask SES about one identity, if the interval allows.

    Returns the identity either way. A refusal is not an error - the stored
    answer is still the answer, and ``last_checked_at`` says how old it is.
    """
    allowed = await check_is_allowed(
        redis, f"identity:{identity.id}", interval_seconds=interval_seconds
    )
    if not allowed:
        return identity

    await check_identity(session, provider_factory, identity)
    return identity


async def check_identity(
    session: AsyncSession,
    provider_factory: ProviderFactory,
    identity: Identity,
) -> Identity:
    """Read the current state from SES and write it onto the row.

    Used by both the manual refresh and the scheduled job. A failure is recorded
    rather than raised: the job runs over many identities and one unreachable
    domain must not stop the rest.
    """
    provider = provider_factory(identity.region)

    try:
        status = await provider.get_identity_status(identity.value)
    except APIError as error:
        identity.last_error = error.message
        identity.last_checked_at = utcnow()
        await session.flush()
        logger.info(
            "identity_check_failed",
            identity_id=identity.id,
            error_type=error.error_type.value,
        )
        return identity

    _apply(identity, status)
    await session.flush()
    return identity


async def remove_identity(
    session: AsyncSession,
    provider_factory: ProviderFactory,
    identity: Identity,
) -> bool:
    """Remove a project's identity, and the SES identity if nothing else uses it.

    Returns whether SES was asked to delete anything.

    The order matters. The reference count is taken before the row is deleted,
    and the SES call happens before the caller commits: if SES refuses, the
    exception rolls the whole thing back and the row survives, rather than
    leaving a project that thinks it removed something SES still holds.
    """
    others = await count_other_references(session, identity)
    value, region, identity_id = identity.value, identity.region, identity.id

    await session.delete(identity)
    await session.flush()

    if others:
        logger.info(
            "identity_row_removed",
            identity_id=identity_id,
            remaining_references=others,
        )
        return False

    provider = provider_factory(region)
    await provider.delete_identity(value)
    logger.info("identity_deleted", identity_id=identity_id)
    return True


def is_recheck_due(
    identity: Identity,
    *,
    unverified_seconds: int,
    verified_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Whether the scheduled job should re-ask SES about this identity.

    A verified identity is re-checked far less often, but not never - that rare
    check is what turns "someone deleted our DKIM record" from a mystery outage
    into a visible status change.
    """
    if identity.last_checked_at is None:
        return True

    interval = verified_seconds if identity.is_verified else unverified_seconds
    elapsed = ((now or utcnow()) - identity.last_checked_at).total_seconds()
    return elapsed >= interval


async def identities_due(
    session: AsyncSession,
    *,
    unverified_seconds: int,
    verified_seconds: int,
    now: datetime | None = None,
) -> list[Identity]:
    """Every identity the scheduled job should re-check on this pass.

    Filtered in Python rather than SQL because the interval depends on the row's
    own status. The set is small - identities are created by hand, one at a
    time - so the simpler expression is worth more than the query.
    """
    result = await session.scalars(select(Identity))
    return [
        identity
        for identity in result
        if is_recheck_due(
            identity,
            unverified_seconds=unverified_seconds,
            verified_seconds=verified_seconds,
            now=now,
        )
    ]


def _apply(identity: Identity, status: IdentityStatus) -> None:
    """Write a provider answer onto the row."""
    identity.identity_type = status.identity_type.value
    identity.verification_status = status.verification_status.value
    identity.dkim_status = status.dkim_status.value if status.dkim_status else None
    identity.mail_from_status = status.mail_from_status.value if status.mail_from_status else None
    identity.dkim_tokens = list(status.dkim_tokens)
    identity.last_checked_at = utcnow()
    identity.last_error = None
