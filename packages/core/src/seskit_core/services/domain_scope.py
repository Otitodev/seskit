"""Which sent mail belongs to a domain.

A domain's page shows what was sent from it. Deciding *which* mail that is has
one rule, and it lives here so the counts, the chart and the list cannot
disagree about it.

**A verified domain covers its subdomains, unless a more specific identity of
the same project exists.** That is how SES treats it: verifying ``example.com``
lets you send as anything on ``example.com`` and on ``mail.example.com``. So mail
from ``a@mail.example.com`` belongs to ``mail.example.com`` if that has been
added, and to ``example.com`` otherwise. The longest match wins, and nothing is
counted twice across the domains of one project.

**An email-address identity covers that address and nothing else.** It sits
beside the domains rather than under one, so a domain's page still includes mail
sent from an address of its own.

The rule is written twice, as :meth:`DomainScope.covers` in Python and as
:meth:`DomainScope.sent_from` in SQL, because one is what a test can read and the
other is what a page can afford. ``tests/test_domain_scope.py`` runs the same
cases through both.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.addressing import domain_of
from seskit_core.email.message import bare_address
from seskit_core.models import Email, EmailStatus, Identity
from seskit_core.providers.types import IdentityType


def _like_escape(text: str) -> str:
    """Escape ``%``, ``_`` and the escape character itself for a LIKE pattern."""
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _within(domain: str) -> ColumnElement[bool]:
    """The domain itself or anything beneath it, as SQL."""
    return or_(
        Email.from_domain == domain,
        Email.from_domain.like(f"%.{_like_escape(domain)}", escape="\\"),
    )


def _under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith(f".{parent}")


@dataclass(frozen=True, slots=True)
class DomainScope:
    """One identity, and the part of its project's mail that belongs to it."""

    value: str
    is_address: bool = False
    #: Domain identities of the same project that sit beneath this one. Their
    #: mail belongs to them, not to this domain.
    narrower: tuple[str, ...] = ()

    def covers(self, from_address: str) -> bool:
        """Whether mail sent from this address belongs to the scope."""
        if self.is_address:
            return bare_address(from_address) == self.value
        domain = domain_of(from_address)
        if domain is None or not _under(domain, self.value):
            return False
        return not any(_under(domain, other) for other in self.narrower)

    def sent_from(self) -> ColumnElement[bool]:
        """The same rule, as a condition on ``emails``."""
        if self.is_address:
            lowered = func.lower(Email.from_address)
            escaped = _like_escape(self.value)
            return or_(
                lowered == self.value,
                # "Display Name <a@b.com>": the mailbox is the part in brackets.
                lowered.like(f"%<{escaped}>", escape="\\"),
            )
        condition = _within(self.value)
        for other in self.narrower:
            condition = and_(condition, not_(_within(other)))
        return condition


async def scope_for(session: AsyncSession, identity: Identity) -> DomainScope:
    """The scope of one identity, with the identities that narrow it.

    Looks at the identity's own project only. Another project's domains have no
    bearing on what this project sent.
    """
    value = identity.value.lower()
    if identity.identity_type == IdentityType.EMAIL_ADDRESS.value:
        return DomainScope(value=value, is_address=True)

    others = await session.scalars(
        select(Identity.value).where(
            Identity.project_id == identity.project_id,
            Identity.identity_type == IdentityType.DOMAIN.value,
        )
    )
    return DomainScope(value=value, narrower=_narrower(value, others))


def _narrower(value: str, domains: Iterable[str]) -> tuple[str, ...]:
    """The domains from ``domains`` that sit beneath ``value``."""
    return tuple(
        sorted(
            {
                other.lower()
                for other in domains
                if other.lower() != value and _under(other.lower(), value)
            }
        )
    )


def scopes_for(identities: Sequence[Identity]) -> dict[str, DomainScope]:
    """The scope of each identity, keyed by id, from a list already in hand.

    For a page showing all of a project's identities at once: the same answer as
    :func:`scope_for` for each, without a query per identity. ``identities`` must
    be the whole project's, or a domain would claim mail that belongs to one
    that was left out.
    """
    domains = [i.value for i in identities if i.identity_type == IdentityType.DOMAIN.value]
    scopes: dict[str, DomainScope] = {}
    for identity in identities:
        value = identity.value.lower()
        if identity.identity_type == IdentityType.EMAIL_ADDRESS.value:
            scopes[identity.id] = DomainScope(value=value, is_address=True)
        else:
            scopes[identity.id] = DomainScope(value=value, narrower=_narrower(value, domains))
    return scopes


async def sent_since(
    session: AsyncSession,
    project_id: str,
    scopes: Mapping[str, DomainScope],
    since: datetime,
) -> dict[str, int]:
    """How many emails each scope sent since ``since``, in one query.

    Keyed like ``scopes``. Counts mail that was sent, as the Overview's "Sent"
    does.
    """
    if not scopes:
        return {}
    keys = list(scopes)
    columns = [func.count().filter(scopes[key].sent_from()) for key in keys]
    row = (
        await session.execute(
            select(*columns).where(
                Email.project_id == project_id,
                Email.status == EmailStatus.SENT.value,
                Email.sent_at >= since,
            )
        )
    ).one()
    return dict(zip(keys, row, strict=True))


__all__ = ["DomainScope", "scope_for", "scopes_for", "sent_since"]
