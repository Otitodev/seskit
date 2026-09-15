"""Who an SNS message speaks for (§15).

An SNS signature proves that Amazon SNS emitted a message. It does not prove
that *this instance's* topic did. SNS signing certificates are per-region keys
shared by every AWS customer, so anyone with an account can have SNS sign
whatever they like by publishing it to a topic of their own - and the signed
fields (Message, MessageId, Subject, Timestamp, TopicArn, Type) say nothing
about which endpoint the message was delivered to.

So the receiver has to check the topic as well as the signature. Not instead
of it: `sns_signature` still decides whether the bytes are genuine, and this
decides whether they are ours. The two answer different questions and both
have to be yes.

**The binding is the region and the account, not the topic ARN.** Two reasons,
both already settled elsewhere in this package. `count_other_users` in
`services/events.py` matches connections on that pair because it is what
decides whether two connections share physical infrastructure, and a row that
failed halfway through provisioning may have no ARN at all. And the SNS
subscription confirmation arrives *before* provisioning has finished - the
subscribe call fires it, and the topic ARN is not committed until the whole
provisioning returns - so matching on the stored ARN would refuse every
legitimate confirmation and break HTTPS setup outright. The account id is
stored at connect time, long before any of that, and is there when SNS calls.

An SNS topic ARN is ``arn:aws:sns:<region>:<account-id>:<name>``. Region and
account are the two segments the signature already covers, and together they
are exactly the identity a connection records.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import AWSConnection

#: ``arn:<partition>:sns:<region>:<account>:<name>`` - six colon-separated
#: segments, and the partition varies (``aws``, ``aws-cn``, ``aws-us-gov``).
_SEGMENTS = 6
_SERVICE = "sns"


@dataclass(frozen=True, slots=True)
class TopicOrigin:
    """The region and account a topic ARN names."""

    region: str
    account_id: str


def parse_topic_arn(arn: str) -> TopicOrigin | None:
    """The origin of a topic ARN, or ``None`` for anything that is not one.

    Strict about shape and loose about nothing else: a malformed ARN is an
    origin nobody has connected, and the caller treats ``None`` exactly as it
    treats an unknown account. There is no reason to be helpful to a message
    whose ``TopicArn`` is not an ARN.
    """
    parts = arn.split(":")
    if len(parts) != _SEGMENTS or parts[0] != "arn" or parts[2] != _SERVICE:
        return None
    region, account = parts[3], parts[4]
    if not region or not account.isdigit():
        return None
    return TopicOrigin(region=region, account_id=account)


async def connections_for_origin(session: AsyncSession, origin: TopicOrigin) -> list[AWSConnection]:
    """Every project whose AWS account and region match.

    Several, ordinarily: projects in one account and region share a topic and
    a queue by design, so a message from that topic may concern any of them.
    An empty list is the answer for an account this instance never connected.

    Status is deliberately not a filter. A connection in ``error`` - a rotated
    key, an undecryptable secret - still owns its messages, and SES still
    publishes their events; refusing those would lose real delivery history
    while the key is being fixed. It costs nothing in safety: the account id
    was verified against AWS when the project connected, and a stranger cannot
    put a row in this table. The SQS side counts broken rows the same way.
    """
    rows = await session.scalars(
        select(AWSConnection).where(
            AWSConnection.region == origin.region,
            AWSConnection.aws_account_id == origin.account_id,
        )
    )
    return list(rows)


__all__ = ["TopicOrigin", "connections_for_origin", "parse_topic_arn"]
