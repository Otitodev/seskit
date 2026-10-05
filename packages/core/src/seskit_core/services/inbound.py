"""Setting up and removing the plumbing for received mail.

The service layer's share of the work: decide *whether* to create or remove
anything, and record what happened. The creating and removing itself is the
provider's, behind :class:`~seskit_core.providers.InboundProvisioner`, so core
still imports no adapter.

**Two halves, owned differently.** The bucket, topic and queue belong to an AWS
account and region and are shared by every domain in them - recorded on each
project's connection, because that is how a project reaches them. A receipt
rule belongs to one domain - recorded on that identity. Setting up a second
domain adds its rule and leaves the first's plumbing alone; removing a domain
removes its rule and leaves the plumbing for as long as anything still uses it.

**The trap the refcount is for**, the same one ``services.events`` has. Two
projects in one account and region share the plumbing, so tearing it down when
the first project's last domain goes would stop the second project's mail, with
nothing on either screen to say why. Teardown counts the other users first.

**One receiver per domain.** A domain's MX record names a single endpoint in a
single region, so two identities both receiving for it cannot both be right. The
unique index on ``identities`` is what guarantees that; the check here only
makes the refusal friendly. The index is the lock, because a check followed by
an insert cannot hold under two concurrent requests.

**AWS first, the row second**, on every removal. The other way round, a removal
that fails halfway leaves a row saying "not receiving" while the rule is still
in the account - and nothing left to name it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import AWSConnection, Identity
from seskit_core.providers import (
    AWSCredentials,
    InboundInfrastructure,
    InboundProvisioner,
    InboundRule,
    InboundStore,
)
from seskit_core.services.credentials import stored_credentials

logger = get_logger(__name__)

#: Builds a provisioner for a region. Injected, for the same reason as
#: ``ProvisionerFactory``: so this module never imports an adapter.
InboundProvisionerFactory = Callable[[str, AWSCredentials], InboundProvisioner]

#: Builds a reader of stored messages for a region, on one project's credentials.
#: Injected so a route never imports an adapter, and a test can substitute one.
InboundStoreFactory = Callable[[str, AWSCredentials], InboundStore]

#: Every rule name starts with this. It must equal the adapter's
#: ``RULE_NAME_PREFIX``, because the bucket policy admits only rules that carry
#: it - a rule named otherwise would be accepted by SES and its writes refused,
#: losing mail with no error anywhere. Core cannot import the adapter to share
#: the constant, so a test holds the two together.
RULE_NAME_PREFIX = "seskit-"

#: What S3 allows for a bucket name.
_MAX_BUCKET_NAME = 63
_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]*[a-z0-9]")


# ------------------------------------------------------------------- names ---


def queue_name_for(prefix: str) -> str:
    """The SQS queue received mail is announced on."""
    return f"{prefix}-inbound"


def topic_name_for(prefix: str) -> str:
    return f"{prefix}-inbound"


def bucket_name_for(prefix: str, *, account_id: str, region: str) -> str:
    """The bucket for one account and region.

    S3 bucket names are global across all of AWS, so the account and region are
    in it: two instances with the same prefix in different accounts must not
    fight over one name.
    """
    name = f"{prefix}-inbound-{account_id}-{region}".lower()
    if len(name) > _MAX_BUCKET_NAME or not _BUCKET_NAME.fullmatch(name):
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "EVENT_RESOURCE_PREFIX is too long, or has characters an S3 bucket name "
            "cannot contain. Use up to about 25 lowercase letters, digits and hyphens.",
        )
    return name


def rule_name_for(prefix: str, domain: str) -> str:
    """The receipt rule for one domain.

    A digest of the prefix and the domain is in it so that two instances sharing
    an account cannot collide on a rule name, and the domain itself is in it so
    a person reading the SES console can tell which is which. Starts with
    :data:`RULE_NAME_PREFIX` and ends alphanumerically, which SES requires.
    """
    digest = hashlib.sha1(f"{prefix}:{domain}".encode(), usedforsecurity=False).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:40]
    return f"{RULE_NAME_PREFIX}{digest}-{slug}".rstrip("-")


# ----------------------------------------------------------------- counting ---


async def _receiving_here(
    session: AsyncSession, connection: AWSConnection, *, excluding: str | None = None
) -> int:
    """How many of this project's identities receive mail."""
    query = (
        select(func.count())
        .select_from(Identity)
        .where(
            Identity.project_id == connection.project_id,
            Identity.inbound_rule_name.is_not(None),
        )
    )
    if excluding is not None:
        query = query.where(Identity.id != excluding)
    return int(await session.scalar(query) or 0)


async def count_other_receivers(session: AsyncSession, connection: AWSConnection) -> int:
    """How many identities in *other* projects receive through the same plumbing.

    Matched on the account and region, because that is what makes two
    connections point at the same physical bucket, topic and queue - not on the
    project, and not on a stored ARN, which a row that failed halfway through
    provisioning may not have.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Identity)
        .join(AWSConnection, AWSConnection.project_id == Identity.project_id)
        .where(
            Identity.inbound_rule_name.is_not(None),
            AWSConnection.project_id != connection.project_id,
            AWSConnection.aws_account_id == connection.aws_account_id,
            AWSConnection.region == connection.region,
        )
    )
    return int(total or 0)


async def has_inbound_to_remove(session: AsyncSession, connection: AWSConnection) -> bool:
    """Whether disconnecting this project would leave receiving plumbing behind.

    A kept bucket does not count: it holds mail the user may want, and removing
    the connection is not a reason to touch it.
    """
    return connection.inbound_enabled or await _receiving_here(session, connection) > 0


# -------------------------------------------------------------------- setup ---


async def setup_receiving(
    session: AsyncSession,
    factory: InboundProvisionerFactory,
    connection: AWSConnection,
    identity: Identity,
    *,
    resource_prefix: str,
    retention_days: int,
    secret_key: str,
) -> Identity:
    """Start receiving mail for one domain.

    Idempotent at both layers: the provisioner converges on the same resources
    and this rewrites the same columns. Running it again is how a user repairs a
    rule they deleted by hand in the console, and how a changed retention is
    applied.
    """
    if not identity.is_domain:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "Only a domain can receive mail. An email address verifies a sender, "
            "and has no MX record to point here.",
        )
    if not identity.is_verified:
        raise APIError(
            ErrorType.DOMAIN_NOT_VERIFIED,
            f"{identity.value} is not verified yet. Receiving mail needs a verified domain.",
        )
    if identity.region != connection.region:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            f"{identity.value} is verified in {identity.region}, but this project is "
            f"connected to {connection.region}. Mail is received in the connection's region.",
        )

    credentials = stored_credentials(connection, secret_key=secret_key)
    rule_name = rule_name_for(resource_prefix, identity.value)
    bucket = bucket_name_for(
        resource_prefix, account_id=connection.aws_account_id, region=connection.region
    )

    before = (
        identity.inbound_rule_name,
        identity.inbound_rule_set,
        identity.inbound_rule_set_created,
    )
    await _claim_domain(session, identity, rule_name)

    try:
        provisioner = factory(connection.region, credentials)
        infrastructure = await provisioner.provision_inbound(
            bucket_name=bucket,
            topic_name=topic_name_for(resource_prefix),
            queue_name=queue_name_for(resource_prefix),
            retention_days=retention_days,
        )
        # Recorded as soon as it exists, and kept if the rule below then fails:
        # the bucket, topic and queue are really in the account, and a row that
        # forgot them is one teardown could never find.
        connection.record_inbound_infrastructure(infrastructure)

        rule = await provisioner.add_inbound_rule(
            infrastructure, domain=identity.value, rule_name=rule_name
        )
    except Exception:
        # Give the domain back. The claim above was written before AWS was
        # called, so that the unique index could decide a race; if AWS then
        # refuses, nothing is receiving, and the identity must not say it is.
        #
        # This matters beyond tidiness. The routes that call this deliberately
        # do not roll back on an error - a rollback expires every loaded object
        # and the page then 500s instead of showing the message - so without
        # this the same render would show "Receiving: on" beside the reason it
        # is not. A repair run puts back what was there, not blank.
        (
            identity.inbound_rule_name,
            identity.inbound_rule_set,
            identity.inbound_rule_set_created,
        ) = before
        await session.flush()
        raise

    identity.inbound_rule_name = rule.name
    identity.inbound_rule_set = rule.rule_set
    identity.inbound_rule_set_created = rule.created_rule_set
    await session.flush()

    logger.info(
        "receiving_set_up",
        project_id=connection.project_id,
        identity_id=identity.id,
        region=connection.region,
    )
    return identity


async def _claim_domain(session: AsyncSession, identity: Identity, rule_name: str) -> None:
    """Take the domain, before anything is built in AWS.

    The name is written and flushed first so that the unique index decides a
    race - two requests enabling the same domain serialise on it, and the loser
    fails here, before it has created a rule in somebody's account. Inside a
    savepoint, so losing does not roll back the request that lost.
    """
    taken = await session.scalar(
        select(func.count())
        .select_from(Identity)
        .where(
            Identity.value == identity.value,
            Identity.inbound_rule_name.is_not(None),
            Identity.id != identity.id,
        )
    )
    if taken:
        raise _already_receiving(identity.value)

    previous = identity.inbound_rule_name
    try:
        async with session.begin_nested():
            identity.inbound_rule_name = rule_name
            await session.flush()
    except IntegrityError as exc:
        identity.inbound_rule_name = previous
        raise _already_receiving(identity.value) from exc


def _already_receiving(domain: str) -> APIError:
    return APIError(
        ErrorType.INVALID_REQUEST,
        f"{domain} already receives mail for another project or region. A domain's MX "
        "record can only point at one place.",
    )


# ----------------------------------------------------------------- teardown ---


async def teardown_receiving(
    session: AsyncSession,
    factory: InboundProvisionerFactory,
    connection: AWSConnection,
    identity: Identity,
    *,
    secret_key: str,
) -> None:
    """Stop receiving for one domain, and release the plumbing if nothing else
    still uses it.
    """
    if not identity.receives_mail:
        return

    provisioner = factory(connection.region, stored_credentials(connection, secret_key=secret_key))

    # AWS first, the row second.
    await provisioner.remove_inbound_rule(
        InboundRule(
            name=identity.inbound_rule_name or "",
            rule_set=identity.inbound_rule_set or "",
            created_rule_set=identity.inbound_rule_set_created,
        )
    )
    identity.inbound_rule_name = None
    identity.inbound_rule_set = None
    identity.inbound_rule_set_created = False
    await session.flush()

    logger.info(
        "receiving_stopped",
        project_id=connection.project_id,
        identity_id=identity.id,
    )

    if await _receiving_here(session, connection) == 0:
        await _release_plumbing(session, provisioner, connection)


async def _release_plumbing(
    session: AsyncSession, provisioner: InboundProvisioner, connection: AWSConnection
) -> None:
    """This project no longer uses the bucket, topic and queue.

    The project's columns are cleared either way - it has stopped using them,
    whoever else has not. The resources themselves go only if nobody else is
    using them.
    """
    infrastructure = connection.inbound_infrastructure
    if not infrastructure.exists:
        return

    others = await count_other_receivers(session, connection)
    if others:
        connection.record_inbound_infrastructure(InboundInfrastructure())
        await session.flush()
        logger.info(
            "receiving_kept_for_other_projects",
            project_id=connection.project_id,
            region=connection.region,
            others=others,
        )
        return

    bucket_removed = await provisioner.remove_inbound(infrastructure)

    # A bucket that still holds mail stays, and stays recorded: a later setup
    # finds it again, and the name is not lost with the row that knew it.
    connection.record_inbound_infrastructure(
        InboundInfrastructure()
        if bucket_removed
        else InboundInfrastructure(bucket=infrastructure.bucket)
    )
    await session.flush()
    logger.info(
        "receiving_torn_down",
        project_id=connection.project_id,
        region=connection.region,
        bucket_removed=bucket_removed,
    )


async def teardown_all_receiving(
    session: AsyncSession,
    factory: InboundProvisionerFactory,
    connection: AWSConnection,
    *,
    secret_key: str,
) -> None:
    """Stop receiving for every domain in the project. Used when disconnecting.

    Each domain's rule goes, then the plumbing if nobody else uses it. A bucket
    still holding mail is left in the account and is not recorded anywhere once
    the connection row is gone - which is why it is logged by name.
    """
    identities = await session.scalars(
        select(Identity).where(
            Identity.project_id == connection.project_id,
            Identity.inbound_rule_name.is_not(None),
        )
    )
    for identity in list(identities):
        await teardown_receiving(session, factory, connection, identity, secret_key=secret_key)

    if connection.inbound_enabled:
        # Plumbing with no rules pointing at it: left by a setup that failed
        # between building it and adding the rule.
        provisioner = factory(
            connection.region, stored_credentials(connection, secret_key=secret_key)
        )
        await _release_plumbing(session, provisioner, connection)

    if connection.inbound_bucket:
        logger.info(
            "receiving_bucket_left_in_place",
            project_id=connection.project_id,
            bucket=connection.inbound_bucket,
        )


# -------------------------------------------------------------------- polling ---


@dataclass(frozen=True, slots=True)
class PolledInbox:
    """One queue of received-mail announcements the worker reads, and for whom."""

    region: str
    queue_url: str
    bucket: str
    credentials: AWSCredentials
    #: The projects whose connection points at this queue. A message read here
    #: may only belong to a domain one of them owns.
    project_ids: frozenset[str]


async def distinct_inbound_queues(session: AsyncSession, *, secret_key: str) -> list[PolledInbox]:
    """Every inbound queue that needs polling, with the key that opens it and
    the projects it speaks for.

    Distinct by queue, for the reason ``distinct_event_queues`` is: projects
    sharing an account and region share a queue, and polling it once per project
    would mean several consumers racing for the same messages.

    A connection whose stored key cannot be decrypted is skipped rather than
    raising: one project with a stale key must not stop every other project's
    mail.
    """
    rows = await session.scalars(
        select(AWSConnection)
        .where(
            AWSConnection.inbound_queue_url.is_not(None),
            AWSConnection.inbound_bucket.is_not(None),
        )
        # "connected" sorts before "error", so a working row wins the queue.
        .order_by(AWSConnection.status, AWSConnection.id)
    )

    credentials: dict[tuple[str, str], AWSCredentials] = {}
    buckets: dict[tuple[str, str], str] = {}
    projects: dict[tuple[str, str], set[str]] = {}
    for connection in rows:
        key = (connection.region, connection.inbound_queue_url or "")
        if not key[1]:
            continue
        projects.setdefault(key, set()).add(connection.project_id)
        if key in credentials:
            continue
        try:
            credentials[key] = stored_credentials(connection, secret_key=secret_key)
        except APIError:
            logger.info(
                "inbound_queue_credentials_unusable",
                project_id=connection.project_id,
                region=connection.region,
            )
            continue
        buckets[key] = connection.inbound_bucket or ""

    return [
        PolledInbox(
            region=region,
            queue_url=url,
            bucket=buckets[(region, url)],
            credentials=creds,
            project_ids=frozenset(projects[(region, url)]),
        )
        for (region, url), creds in credentials.items()
    ]
