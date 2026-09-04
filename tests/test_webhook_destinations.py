"""Refusing webhook destinations SESKit must not reach (§16).

The requirement `docs/design/prior-art.md` records as missing from the comparable
project. The danger is a composition, not either half:

- a user may register any URL, including one inside SESKit's own network;
- delivery responses are captured and rendered into the dashboard.

Together those make SESKit an HTTP client an attacker can point at the internal
network and read the answers from. `http://169.254.169.254/latest/meta-data/`
returns cloud instance credentials.

Every test here uses a stubbed resolver, so none of them touch DNS or depend on
what the network answers today. That is also what makes the rebinding test
possible: a real resolver cannot be asked to change its mind on the second call.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from seskit_core.security.destinations import (
    Destination,
    DestinationError,
    DestinationPolicy,
    is_public,
    parse_networks,
    validate,
)

PUBLIC = "93.184.216.34"
STRICT = DestinationPolicy()
LOCAL = DestinationPolicy(allow_private=True, require_https=False)


def resolving_to(*addresses: str) -> object:
    """A resolver that always answers with these addresses."""

    def resolver(host: str) -> Sequence[str]:
        return list(addresses)

    return resolver


async def _validate(url: str, *addresses: str, policy: DestinationPolicy = STRICT) -> Destination:
    return await validate(url, policy=policy, resolver=resolving_to(*addresses or (PUBLIC,)))  # type: ignore[arg-type]


# ------------------------------------------------------------------ accepted ---


async def test_a_public_https_url_is_accepted() -> None:
    destination = await _validate("https://hooks.example.com/seskit", PUBLIC)

    assert destination.host == "hooks.example.com"
    assert str(destination.pinned) == PUBLIC


async def test_a_url_with_a_port_and_query_is_accepted() -> None:
    destination = await _validate("https://hooks.example.com:8443/hook?x=1", PUBLIC)

    assert destination.host == "hooks.example.com"


async def test_a_public_ipv6_address_is_accepted() -> None:
    destination = await _validate(
        "https://[2606:2800:220:1:248:1893:25c8:1946]/x", "2606:2800:220::1"
    )

    assert destination.addresses


# ------------------------------------------------- refused by address ---------


@pytest.mark.parametrize(
    ("label", "address"),
    [
        ("loopback", "127.0.0.1"),
        ("loopback, other than .1", "127.99.4.2"),
        ("private 10/8", "10.0.0.5"),
        ("private 172.16/12", "172.16.4.9"),
        ("private 192.168/16", "192.168.1.10"),
        ("link-local / cloud metadata", "169.254.169.254"),
        # Not a bind address here - it is a destination that must be refused,
        # which is the opposite of what S104 is warning about.
        ("unspecified", "0.0.0.0"),  # noqa: S104
        ("multicast", "224.0.0.1"),
        ("IPv6 loopback", "::1"),
        ("IPv6 unique-local", "fd00::1"),
        ("IPv6 link-local", "fe80::1"),
        # ::ffff:127.0.0.1 is loopback wearing a different hat. As an IPv6
        # address it is not loopback, private or reserved, so a check that
        # forgets to unwrap it lets the whole loopback range straight through.
        ("IPv4-mapped loopback", "::ffff:127.0.0.1"),
        ("IPv4-mapped metadata", "::ffff:169.254.169.254"),
    ],
)
async def test_an_internal_address_is_refused(label: str, address: str) -> None:
    with pytest.raises(DestinationError):
        await _validate("https://internal.example.com/x", address)


async def test_the_cloud_metadata_service_is_refused() -> None:
    """Named on its own because it is the specific thing that turns this into a
    credential disclosure rather than a curiosity.
    """
    with pytest.raises(DestinationError):
        await _validate("https://metadata.example.com/latest/meta-data/", "169.254.169.254")


async def test_a_literal_internal_address_is_refused() -> None:
    """No hostname to resolve - the URL names the address directly."""
    with pytest.raises(DestinationError):
        await _validate("https://127.0.0.1/x", "127.0.0.1")


async def test_one_bad_answer_refuses_the_whole_name() -> None:
    """A host answering with both a public and a private address must be
    refused. Otherwise whichever answer the HTTP client happened to pick would
    decide whether the check held.
    """
    with pytest.raises(DestinationError):
        await _validate("https://split.example.com/x", PUBLIC, "10.0.0.1")


async def test_a_name_that_resolves_to_nothing_is_refused() -> None:
    with pytest.raises(DestinationError):
        await validate("https://nowhere.example.com/x", policy=STRICT, resolver=lambda host: [])


# -------------------------------------------------- refused by the URL --------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",  # plain HTTP
        "ftp://hooks.example.com/x",
        "file:///etc/passwd",
        "gopher://hooks.example.com/x",
        "",
        "not a url at all",
        "https://",
        # Credentials in a URL are a well-worn way to make it read as one host
        # and resolve to another.
        "https://good.example.com@evil.example.com/x",
        "https://user:pass@hooks.example.com/x",
    ],
)
async def test_a_url_that_should_never_be_accepted_is_refused(url: str) -> None:
    with pytest.raises(DestinationError):
        await _validate(url, PUBLIC)


async def test_an_absurdly_long_url_is_refused() -> None:
    with pytest.raises(DestinationError):
        await _validate("https://hooks.example.com/" + "x" * 3000, PUBLIC)


async def test_every_refusal_says_the_same_thing() -> None:
    """Naming which rule refused would tell whoever is probing the form exactly
    which internal range to try next, and the user's remedy is identical in
    every case.
    """
    messages = set()
    for url, address in [
        ("http://hooks.example.com/x", PUBLIC),
        ("https://hooks.example.com/x", "127.0.0.1"),
        ("https://hooks.example.com/x", "169.254.169.254"),
        ("https://user:pass@hooks.example.com/x", PUBLIC),
    ]:
        try:
            await _validate(url, address)
        except DestinationError as error:
            messages.add(error.message)

    assert len(messages) == 1


# ------------------------------------------------------- DNS rebinding --------


async def test_the_check_runs_against_the_resolved_address_every_time() -> None:
    """The reason validation is not done once at registration.

    The same hostname answers publicly the first time and with loopback the
    second. A check that trusted the string after the first pass would deliver
    to 127.0.0.1 and put the response in the dashboard.
    """
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def rebinding(host: str) -> Sequence[str]:
        return next(answers)

    url = "https://rebind.example.com/x"

    assert (await validate(url, policy=STRICT, resolver=rebinding)).host == "rebind.example.com"

    with pytest.raises(DestinationError):
        await validate(url, policy=STRICT, resolver=rebinding)


async def test_the_resolved_address_is_returned_for_pinning() -> None:
    """Resolving, checking, and then letting the HTTP client resolve the name
    again would reopen the window this closes. The caller connects to what was
    checked.
    """
    destination = await _validate("https://hooks.example.com/x", PUBLIC)

    assert str(destination.pinned) == PUBLIC


# ------------------------------------------------------------- policy --------


async def test_local_development_may_reach_localhost() -> None:
    """A receiver on localhost is how anyone tries this feature at all, and
    self-hosted software that cannot be tested locally will not be adopted.
    """
    assert (
        await _validate("http://localhost:9000/hook", "127.0.0.1", policy=LOCAL)
    ).host == "localhost"


async def test_an_allowlisted_range_is_reachable_in_production() -> None:
    """The deliberate escape hatch: a self-hosted deployment with a genuine
    internal destination writes the CIDR into the configuration.
    """
    policy = DestinationPolicy(allowed_networks=parse_networks("10.10.0.0/16"))

    assert await _validate("https://internal.example.com/x", "10.10.4.5", policy=policy)


async def test_an_allowlist_does_not_permit_the_range_next_door() -> None:
    """The check that makes the previous test mean something."""
    policy = DestinationPolicy(allowed_networks=parse_networks("10.10.0.0/16"))

    with pytest.raises(DestinationError):
        await _validate("https://internal.example.com/x", "10.11.4.5", policy=policy)


async def test_an_allowlist_does_not_open_plain_http() -> None:
    """Permitting an address range says nothing about permitting an unencrypted
    connection to it.
    """
    policy = DestinationPolicy(allowed_networks=parse_networks("10.10.0.0/16"))

    with pytest.raises(DestinationError):
        await _validate("http://internal.example.com/x", "10.10.4.5", policy=policy)


def test_the_default_policy_allows_nothing_internal() -> None:
    """Defaults are the safe ones, so a configuration file that omits these
    settings gets the strict behaviour rather than the permissive one.
    """
    policy = DestinationPolicy()

    assert policy.allow_private is False
    assert policy.require_https is True
    assert policy.allowed_networks == ()


# --------------------------------------------------------------- parsing -----


def test_cidrs_parse_from_a_comma_separated_list() -> None:
    networks = parse_networks(" 10.0.0.0/8 ,192.168.1.0/24, ")

    assert len(networks) == 2


def test_an_empty_setting_parses_to_nothing() -> None:
    assert parse_networks("") == ()


def test_a_malformed_cidr_raises_rather_than_being_skipped() -> None:
    """Silently ignoring a typo would leave an operator believing they had
    permitted a range they had not, and the symptom would be a webhook that
    simply never arrives.
    """
    with pytest.raises(ValueError):
        parse_networks("10.0.0.0/8, not-a-cidr")


# ----------------------------------------------------------------- units -----


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("8.8.8.8", True),
        ("2606:2800:220::1", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_classifies_addresses(address: str, public: bool) -> None:
    import ipaddress

    assert is_public(ipaddress.ip_address(address)) is public
