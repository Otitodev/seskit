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
