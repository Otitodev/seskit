"""Fakes for received mail, for tests that go through the real application.

Substituted for ``S3InboundStore`` and ``SESInboundProvisioner`` so that no test
can reach AWS by forgetting to override one. They satisfy the same Protocols as
the real adapters, so a signature drift breaks here as well as in production.

Not the fakes ``test_inbound_service`` uses. Those record calls in class-level
state to make the refcount observable; these are the quiet versions that sit
behind every ``app_client`` test, where nothing should be happening at all.
"""

from __future__ import annotations

from typing import ClassVar

from seskit_core.errors import APIError, ErrorType
from seskit_core.providers import AWSCredentials, InboundInfrastructure, InboundRule

EXPIRED = "That message is no longer in storage."


class FakeInboundStore:
    """Stored messages by ``(bucket, key)``, and a record of every fetch."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fetches: list[tuple[str, str]] = []
        #: Raised by every fetch when set, to stand in for a permission or a
        #: network failure.
        self.error: APIError | None = None
        #: The region and key the last build was handed. Recorded because "does
        #: this project read its mail with its own key?" is a thing a test asks.
        self.built_with: tuple[str, AWSCredentials] | None = None

    def put(self, bucket: str, key: str, raw: bytes) -> None:
        self.objects[(bucket, key)] = raw

    def factory(self, region: str, credentials: AWSCredentials) -> FakeInboundStore:
        self.built_with = (region, credentials)
        return self

    async def fetch_message(self, *, bucket: str, key: str) -> bytes:
        self.fetches.append((bucket, key))
        if self.error is not None:
            raise self.error
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise APIError(ErrorType.NOT_FOUND, EXPIRED) from None


class FakeInboundProvisioner:
    """A provisioner that builds nothing and says it did."""

    calls: ClassVar[list[str]] = []
    #: Raised by ``provision_inbound`` when set, to stand in for AWS refusing - a
    #: missing permission, say.
    error: ClassVar[APIError | None] = None
    #: Raised by ``remove_inbound_rule`` when set.
    remove_error: ClassVar[APIError | None] = None

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.error = None
        cls.remove_error = None

    def __init__(self, region: str, credentials: AWSCredentials | None = None) -> None:
        self.region = region
        self.credentials = credentials

    async def provision_inbound(
        self, *, bucket_name: str, topic_name: str, queue_name: str, retention_days: int
    ) -> InboundInfrastructure:
        if FakeInboundProvisioner.error is not None:
            raise FakeInboundProvisioner.error
        FakeInboundProvisioner.calls.append("provision")
        return InboundInfrastructure(
            bucket=bucket_name,
            topic_arn=f"arn:aws:sns:{self.region}:123456789012:{topic_name}",
            queue_url=f"https://sqs.{self.region}.amazonaws.com/123456789012/{queue_name}",
            queue_arn=f"arn:aws:sqs:{self.region}:123456789012:{queue_name}",
            subscription_arn=f"arn:aws:sns:{self.region}:123456789012:{topic_name}:sub",
        )

    async def add_inbound_rule(
        self, infrastructure: InboundInfrastructure, *, domain: str, rule_name: str
    ) -> InboundRule:
        FakeInboundProvisioner.calls.append("add_rule")
        return InboundRule(name=rule_name, rule_set="seskit-inbound", created_rule_set=True)

    async def remove_inbound_rule(self, rule: InboundRule) -> None:
        if FakeInboundProvisioner.remove_error is not None:
            raise FakeInboundProvisioner.remove_error
        FakeInboundProvisioner.calls.append("remove_rule")

    async def remove_inbound(self, infrastructure: InboundInfrastructure) -> bool:
        FakeInboundProvisioner.calls.append("remove")
        return True
