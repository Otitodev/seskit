"""AWS regions where SES is available.

A curated list rather than free text: not every AWS region offers SES, and a
typo in a region name surfaces as a connection failure whose message is about
endpoints rather than about the typo.

The list is a convenience for the picker, not the validator. The ``GetAccount``
call against the chosen region is the real proof, and it is what sets the
connection's status - so a region that is missing here because AWS added it
after this was written still works if a user configures it directly.
"""

from __future__ import annotations

from typing import Final

#: (region code, human label). Ordered by continent then code, which is how the
#: AWS console groups them, so the list reads the way users expect.
SES_REGIONS: Final[tuple[tuple[str, str], ...]] = (
    ("us-east-1", "US East (N. Virginia)"),
    ("us-east-2", "US East (Ohio)"),
    ("us-west-1", "US West (N. California)"),
    ("us-west-2", "US West (Oregon)"),
    ("ca-central-1", "Canada (Central)"),
    ("sa-east-1", "South America (São Paulo)"),
    ("eu-west-1", "Europe (Ireland)"),
    ("eu-west-2", "Europe (London)"),
    ("eu-west-3", "Europe (Paris)"),
    ("eu-central-1", "Europe (Frankfurt)"),
    ("eu-north-1", "Europe (Stockholm)"),
    ("eu-south-1", "Europe (Milan)"),
    ("af-south-1", "Africa (Cape Town)"),
    ("me-south-1", "Middle East (Bahrain)"),
    ("il-central-1", "Israel (Tel Aviv)"),
    ("ap-south-1", "Asia Pacific (Mumbai)"),
    ("ap-northeast-1", "Asia Pacific (Tokyo)"),
    ("ap-northeast-2", "Asia Pacific (Seoul)"),
    ("ap-northeast-3", "Asia Pacific (Osaka)"),
    ("ap-southeast-1", "Asia Pacific (Singapore)"),
    ("ap-southeast-2", "Asia Pacific (Sydney)"),
    ("ap-southeast-3", "Asia Pacific (Jakarta)"),
)

SES_REGION_CODES: Final[frozenset[str]] = frozenset(code for code, _ in SES_REGIONS)


def is_known_region(region: str) -> bool:
    """Whether this is a region we list.

    Used to catch an obvious typo before spending a round trip on it - never to
    refuse a region outright, since AWS adds regions faster than this list is
    updated.
    """
    return region in SES_REGION_CODES


#: Where SES can *receive* mail, as opposed to send it. Checked against the
#: "Email Receiving endpoints" table in the AWS General Reference on
#: 2026-09-29. Today it is the same set as :data:`SES_REGION_CODES`, but it is
#: written out rather than aliased: AWS has added sending regions without
#: receiving in them before (Hyderabad, Zurich, Malaysia, the UAE and Calgary
#: all send and cannot receive), so the two lists will drift apart the next time
#: it happens, and an alias would hide which of them a change belongs in.
RECEIVING_REGION_CODES: Final[frozenset[str]] = frozenset(
    {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ca-central-1",
        "sa-east-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "eu-north-1",
        "eu-south-1",
        "af-south-1",
        "me-south-1",
        "il-central-1",
        "ap-south-1",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
    }
)


def supports_receiving(region: str) -> bool:
    """Whether SES can receive mail in this region.

    Unlike :func:`is_known_region` this *is* a gate. Sending in an unlisted
    region might work; receiving in one that has no inbound endpoint cannot,
    and there is nothing for a user to try.
    """
    return region in RECEIVING_REGION_CODES
