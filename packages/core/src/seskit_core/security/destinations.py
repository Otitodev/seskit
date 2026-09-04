"""Deciding whether SESKit may POST to a URL a user typed (§16).

This is the requirement `docs/design/prior-art.md` records as *missing* from the
comparable project, and the reason it matters is a composition rather than
either half on its own:

- a user can register any URL, including one inside the network SESKit runs in;
- delivery responses are captured and rendered back into the dashboard.

Separately those are a webhook feature and a debugging aid. Together they are a
**read primitive against the internal network**: register
``http://169.254.169.254/latest/meta-data/``, receive an event, read the cloud
instance's credentials off the delivery log. The dashboard turns SESKit into the
attacker's HTTP client.

**Validation runs twice, and the second time is the one that counts.**
Registration-time validation is a courtesy - it puts the error on the form
instead of in a log an hour later. Delivery-time validation against the
*resolved* address is the control, because a hostname is not a destination: a
name that resolved to a public address when it was registered can resolve to
``127.0.0.1`` on the next attempt. Checking the string once and trusting it
forever is exactly what DNS rebinding defeats.

**Self-hosted software has legitimate internal destinations.** A user running
SESKit on their own network may genuinely want to POST to another host on it,
and refusing outright would be wrong for this audience. So the rule is graded:
blocked in production, permitted in local development, and permitted in
production only for address ranges an operator has deliberately written into
``WEBHOOK_ALLOWED_CIDRS``. An escape hatch you have to type is a different thing
from one that is open by default.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Resolves a hostname to addresses. Injected so the tests neither touch DNS nor
#: depend on what the network happens to answer today - and so a test can
#: simulate the rebinding case, where the same name answers differently on the
#: second call.
Resolver = Callable[[str], Sequence[str]]

#: One message for every rejection. Naming which rule refused would tell someone
#: probing the form exactly which internal range to try next, and the user's
#: remedy is the same in every case.
REFUSED_MESSAGE = (
    "That URL cannot be used for webhooks. It must be an https:// address that "
    "resolves to a public host."
)

MAX_URL_LENGTH = 2048


class DestinationError(Exception):
    """The URL is not one SESKit will send to.

    Carries a message safe to render on a form - see ``REFUSED_MESSAGE``.
    """

    def __init__(self, message: str = REFUSED_MESSAGE) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """What this instance is willing to send to.

    Built from settings rather than read from them directly, so the rules can be
    tested without constructing a whole ``Settings`` object - and so the
    defaults here are the safe ones rather than whatever a config file omitted.
    """

    #: Loopback, private and link-local addresses. True only in local
    #: development, where a webhook receiver on localhost is the normal way to
    #: try the feature at all.
    allow_private: bool = False
    #: Ranges an operator has deliberately permitted. Checked even when
    #: ``allow_private`` is False - that is the point of it.
    allowed_networks: tuple[IPNetwork, ...] = field(default_factory=tuple)
    #: Plain HTTP is refused outside local development: the payload carries a
    #: signature but the body itself is readable by anyone on the path.
    require_https: bool = True


@dataclass(frozen=True, slots=True)
class Destination:
    """A URL that passed, and the addresses it resolved to.

    The addresses are returned rather than discarded so the caller can connect
    to one of them directly. Resolving, checking, and then letting the HTTP
    client resolve the name *again* leaves a window where DNS answers
    differently in between - which is the whole attack.
    """

    url: str
    host: str
    addresses: tuple[IPAddress, ...]

    @property
    def pinned(self) -> IPAddress:
        """The address to actually connect to."""
        return self.addresses[0]


def parse_networks(value: str) -> tuple[IPNetwork, ...]:
    """Parse ``WEBHOOK_ALLOWED_CIDRS`` - a comma-separated list of CIDRs.

    A malformed entry raises rather than being skipped. Silently ignoring a
    typo would leave an operator believing they had permitted a range they had
    not, and the failure would look like a webhook that simply never arrives.
    """
    networks: list[IPNetwork] = []
    for entry in value.split(","):
        text = entry.strip()
        if not text:
            continue
        networks.append(ipaddress.ip_network(text, strict=False))
    return tuple(networks)


def default_resolver(host: str) -> Sequence[str]:
    """Every address a hostname answers with.

    All of them, not just the first: a name that returns one public and one
    private address must be refused, and asking for a single answer would let
    the ordering decide whether the check passes.

    Synchronous, and never called directly from a coroutine - ``validate`` hands
    it to a thread. ``getaddrinfo`` blocks, and a hostname whose nameserver is
    slow would otherwise stall every request the process is serving, not just
    this one.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DestinationError() from exc
    # str() because the sockaddr tuple is typed as a union - IPv6 carries
    # flowinfo and scope_id after the address, so the element type is not
    # narrowed to str on its own.
    return [str(info[4][0]) for info in infos]


def is_public(address: IPAddress) -> bool:
    """Whether an address is one the public internet can route to.

    Deliberately a denylist of properties rather than a list of ranges: the
    ``ipaddress`` module already knows what loopback, private, link-local,
    multicast and reserved mean, in both address families, and reimplementing
    that is how ``::1`` and IPv4-mapped IPv6 get missed.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 is loopback wearing a different hat, and it is not
        # private, loopback or reserved as an IPv6 address.
        address = address.ipv4_mapped

    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def validate(
    url: str,
    *,
    policy: DestinationPolicy,
    resolver: Resolver | None = None,
) -> Destination:
    """Check a webhook URL, resolve it, and check where it actually points.

    Raises :class:`DestinationError` for anything refused. Called at
    registration for the error message and again at every delivery for the
    protection - see the module docstring on why once is not enough.

    Async because DNS is. The default lookup goes to a thread rather than
    blocking the event loop; an injected resolver is called directly, since a
    test's answer is already in memory.
    """
    host = _check_url(url, policy=policy)

    if resolver is None:
        raw = await asyncio.to_thread(default_resolver, host)
    else:
        raw = resolver(host)
    if not raw:
        raise DestinationError()

    addresses: list[IPAddress] = []
    for item in raw:
        try:
            addresses.append(ipaddress.ip_address(item))
        except ValueError as exc:
            raise DestinationError() from exc

    for address in addresses:
        if not _address_allowed(address, policy=policy):
            # *Any* disallowed answer refuses the whole name. A host that
            # returns one public and one private address would otherwise be
            # usable by whichever answer the client happened to pick.
            raise DestinationError()

    return Destination(url=url, host=host, addresses=tuple(addresses))


def _check_url(url: str, *, policy: DestinationPolicy) -> str:
    """Everything decidable from the string alone. Returns the hostname."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise DestinationError()

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise DestinationError() from exc

    if parsed.scheme not in ("http", "https"):
        raise DestinationError()
    if policy.require_https and parsed.scheme != "https":
        raise DestinationError()

    # Credentials in the URL are refused outright. They are a confusing way to
    # write a destination, they end up in logs, and `http://good.example@evil`
    # is a well-worn way to make a URL read as one host and resolve to another.
    if parsed.username or parsed.password:
        raise DestinationError()

    if not parsed.hostname:
        raise DestinationError()

    return parsed.hostname


def _address_allowed(address: IPAddress, *, policy: DestinationPolicy) -> bool:
    """Whether this resolved address may be connected to.

    The allowlist is checked first and independently of ``allow_private``: an
    operator who wrote a CIDR into the configuration has said what they mean,
    and that should hold in production, which is the only place it matters.
    """
    if any(address in network for network in policy.allowed_networks):
        return True
    if is_public(address):
        return True
    return policy.allow_private
