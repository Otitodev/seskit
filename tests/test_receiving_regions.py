"""Where SES can receive mail (inbound email, Phase A).

Sending and receiving are offered in different sets of regions, and the gap is
the point of this file: a project in a region that can send but not receive
must be told so, not shown a switch that cannot work. None of these need a
database.
"""

from __future__ import annotations

import pytest
from seskit_provider_aws_ses import (
    RECEIVING_REGION_CODES,
    SES_REGION_CODES,
    supports_receiving,
)

#: Regions the AWS General Reference lists a *sending* endpoint for and no
#: receiving endpoint, as of 2026-09-29.
SEND_ONLY = ["ap-south-2", "ap-southeast-5", "eu-central-2", "me-central-1", "ca-west-1"]


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "ap-southeast-2", "il-central-1"])
def test_a_receiving_region_is_supported(region: str) -> None:
    assert supports_receiving(region)


@pytest.mark.parametrize("region", [*SEND_ONLY, "us-gov-west-1", "us-gov-east-1"])
def test_a_region_that_can_only_send_is_refused(region: str) -> None:
    assert not supports_receiving(region)


def test_a_typo_is_refused_rather_than_guessed_at() -> None:
    assert not supports_receiving("us-east-11")
    assert not supports_receiving("")


def test_every_receiving_region_is_also_offered_for_sending() -> None:
    """A region can be listed for receiving only if the picker offers it, or a
    project could never be in it.
    """
    assert RECEIVING_REGION_CODES <= SES_REGION_CODES
