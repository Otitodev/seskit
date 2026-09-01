"""Creating and removing event infrastructure (§15).

Against moto, so what is asserted is the state AWS is actually left in - a queue
that exists, a subscription that points at it - rather than that the right boto3
methods were called in the right order, which is a test of the test.

**moto 5.2.3, checked 2026-09-01.** The spike §31 asks for found the same shape
of gap Phase 4 found in ``GetAccount``:

===================================================  ======================
SQS: create, attributes, policy, delete              implemented
SNS: create, subscribe, attributes, unsubscribe      implemented
SESv2 Create/DeleteConfigurationSet                  implemented
SESv2 ``*ConfigurationSetEventDestination`` (all 4)  **not implemented**
===================================================  ======================

Also worth recording: moto raises ``ConfigurationSetAlreadyExistsException``
for a duplicate where SES v2 documents ``AlreadyExistsException``. Both are
tolerated in the adapter, because handling only the one the mock produces would
pass the suite and fail in production.

The four missing calls are covered by a recording client that wraps moto and
intercepts exactly those methods, so their arguments - which event types are
requested, which topic is named - are still asserted. ``test_moto_still_...``
below is the canary that says when this can be deleted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("moto", reason="moto is a dev dependency")

import boto3
from moto import mock_aws
from seskit_core.providers import EventInfrastructure
from seskit_provider_aws_ses import (
    BASE_EVENT_TYPES,
    EVENT_DESTINATION_NAME,
    TRACKING_EVENT_TYPES,
    SESEventProvisioner,
    event_types,
    queue_policy,
)

REGION = "us-east-1"
QUEUE = "seskit-events"
TOPIC = "seskit-events"
CONFIG_SET = "seskit"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials, so a real profile on this machine cannot be used."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


class _RecordingSES:
    """moto's SESv2 client, with the four calls it lacks recorded instead.

    Everything else is delegated, so the configuration set really is created in
    moto and the test can see it. Only the event-destination calls are
    intercepted, and their arguments are kept - those arguments are the whole
    behaviour worth testing here.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def create_configuration_set_event_destination(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {}

    def update_configuration_set_event_destination(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {}

    def delete_configuration_set_event_destination(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs)
        return {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _provisioner(monkeypatch: pytest.MonkeyPatch) -> tuple[SESEventProvisioner, _RecordingSES]:
    provisioner = SESEventProvisioner(REGION)
    recorder = _RecordingSES(boto3.client("sesv2", region_name=REGION))
    monkeypatch.setattr(provisioner, "_ses", lambda: recorder)
    return provisioner, recorder


def _matching_types(call: dict[str, Any]) -> list[str]:
    types = call["EventDestination"]["MatchingEventTypes"]
    return list(types)


# ------------------------------------------------------------------- pure ---


def test_open_and_click_are_not_requested_by_default() -> None:
    """Enabling them rewrites every link in the customer's mail and adds a
    pixel. That is a change to *their* product, and it is opt-in.
    """
    types = event_types(track_opens_and_clicks=False)

    assert "OPEN" not in types
    assert "CLICK" not in types
    assert set(BASE_EVENT_TYPES) <= set(types)


def test_opting_in_adds_open_and_click() -> None:
    types = event_types(track_opens_and_clicks=True)

    assert set(TRACKING_EVENT_TYPES) <= set(types)


def test_the_queue_policy_admits_one_topic_only() -> None:
    """Without the SourceArn condition any SNS topic in any account could post
    into this queue, and SESKit would record whatever bounces it was told about.
    """
    policy = json.loads(queue_policy(queue_arn="arn:queue", topic_arn="arn:topic"))
    statement = policy["Statement"][0]

    assert statement["Condition"]["ArnEquals"]["aws:SourceArn"] == "arn:topic"
    assert statement["Action"] == "sqs:SendMessage"
    assert statement["Resource"] == "arn:queue"


# --------------------------------------------------------------- provision ---


async def test_provisioning_creates_the_queue_topic_and_subscription(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        sqs = boto3.client("sqs", region_name=REGION)
        sns = boto3.client("sns", region_name=REGION)

        assert sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"] == result.queue_url
        assert result.topic_arn in [topic["TopicArn"] for topic in sns.list_topics()["Topics"]]
        subscriptions = sns.list_subscriptions_by_topic(TopicArn=result.topic_arn)
        assert [sub["Endpoint"] for sub in subscriptions["Subscriptions"]] == [result.queue_arn]


async def test_the_queue_is_left_writable_by_its_topic(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A topic subscribed to a queue it may not write to drops messages without
    erroring anywhere - the hardest kind of failure to notice.
    """
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        sqs = boto3.client("sqs", region_name=REGION)
        attributes = sqs.get_queue_attributes(QueueUrl=result.queue_url, AttributeNames=["Policy"])
        policy = json.loads(attributes["Attributes"]["Policy"])

        assert policy["Statement"][0]["Condition"]["ArnEquals"]["aws:SourceArn"] == result.topic_arn


async def test_raw_message_delivery_stays_off(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing one.

    Raw delivery strips the SNS envelope, and the envelope's MessageId is what
    deduplication keys on - the event body is identical across redeliveries.
    Turning it on looks like a simplification and silently breaks exactly-once.
    """
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        sns = boto3.client("sns", region_name=REGION)
        attributes = sns.get_subscription_attributes(SubscriptionArn=result.subscription_arn)

        assert attributes["Attributes"].get("RawMessageDelivery", "false") == "false"


async def test_the_configuration_set_is_created(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it SES publishes nothing at all, however well the rest is wired."""
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        ses = boto3.client("sesv2", region_name=REGION)
        assert CONFIG_SET in ses.list_configuration_sets()["ConfigurationSets"]


async def test_the_destination_points_at_the_topic_and_omits_tracking(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        assert len(recorder.created) == 1
        call = recorder.created[0]
        assert call["EventDestination"]["SnsDestination"]["TopicArn"] == result.topic_arn
        assert "OPEN" not in _matching_types(call)
        assert result.tracks_opens_and_clicks is False


async def test_opting_into_tracking_requests_open_and_click(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name=QUEUE,
            topic_name=TOPIC,
            configuration_set=CONFIG_SET,
            track_opens_and_clicks=True,
        )

        assert set(TRACKING_EVENT_TYPES) <= set(_matching_types(recorder.created[0]))
        assert result.tracks_opens_and_clicks is True


async def test_an_https_endpoint_gets_its_own_subscription(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second transport. SQS suits a laptop behind NAT; a deployment with a
    public address may prefer to be pushed to.
    """
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        result = await provisioner.provision_events(
            queue_name="",
            topic_name=TOPIC,
            configuration_set=CONFIG_SET,
            https_endpoint="https://mail.example.com/v1/events/ses",
        )

        sns = boto3.client("sns", region_name=REGION)
        subscriptions = sns.list_subscriptions_by_topic(TopicArn=result.topic_arn)

        assert [sub["Protocol"] for sub in subscriptions["Subscriptions"]] == ["https"]
        assert result.queue_url == ""
        assert result.https_subscription_arn


async def test_provisioning_twice_converges(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who clicks the button again because nothing seemed to happen must
    not end up with two of everything - and must not get an error either.
    """
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)

        first = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )
        second = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        sns = boto3.client("sns", region_name=REGION)
        ses = boto3.client("sesv2", region_name=REGION)

        assert first.queue_url == second.queue_url
        assert first.topic_arn == second.topic_arn
        assert len(sns.list_topics()["Topics"]) == 1
        assert ses.list_configuration_sets()["ConfigurationSets"].count(CONFIG_SET) == 1
        # The duplicate configuration set was tolerated rather than fatal.
        assert len(recorder.created) == 2


# ---------------------------------------------------------------- teardown ---


async def test_teardown_removes_what_was_created(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)
        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        await provisioner.remove_events(result)

        sns = boto3.client("sns", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        ses = boto3.client("sesv2", region_name=REGION)

        assert sns.list_topics()["Topics"] == []
        assert sqs.list_queues().get("QueueUrls", []) == []
        assert CONFIG_SET not in ses.list_configuration_sets()["ConfigurationSets"]
        assert recorder.deleted[0]["EventDestinationName"] == EVENT_DESTINATION_NAME


async def test_teardown_touches_nothing_it_did_not_create(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SESKit owns resources in someone else's account now. A disconnect that
    deletes by guessing at a name reaches things the user made themselves.
    """
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        sns = boto3.client("sns", region_name=REGION)
        theirs_url = sqs.create_queue(QueueName="their-important-queue")["QueueUrl"]
        theirs_topic = sns.create_topic(Name="their-important-topic")["TopicArn"]

        provisioner, _ = _provisioner(monkeypatch)
        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        await provisioner.remove_events(result)

        assert sqs.get_queue_url(QueueName="their-important-queue")["QueueUrl"] == theirs_url
        assert theirs_topic in [topic["TopicArn"] for topic in sns.list_topics()["Topics"]]


async def test_teardown_of_something_already_gone_is_not_an_error(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who deleted the queue by hand in the console still needs to be
    able to disconnect. A teardown that raises leaves the row behind forever.
    """
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        await provisioner.remove_events(
            EventInfrastructure(
                configuration_set=CONFIG_SET,
                topic_arn=f"arn:aws:sns:{REGION}:123456789012:gone",
                queue_url=f"https://sqs.{REGION}.amazonaws.com/123456789012/gone",
                subscription_arn=f"arn:aws:sns:{REGION}:123456789012:gone:abc",
            )
        )


async def test_an_unconfirmed_https_subscription_is_not_unsubscribed(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNS returns "PendingConfirmation" instead of an ARN until the endpoint
    answers. Passing that string to Unsubscribe is an error, not a no-op.
    """
    with mock_aws():
        provisioner, _ = _provisioner(monkeypatch)

        await provisioner.remove_events(
            EventInfrastructure(https_subscription_arn="PendingConfirmation")
        )


# ---------------------------------------------------------------- tracking ---


async def test_turning_tracking_on_updates_the_existing_destination(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmed against the SES documentation rather than from memory: adding
    OPEN and CLICK to MatchingEventTypes is the entire mechanism. Only a custom
    tracking domain (§10, V1.1) needs anything more.
    """
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)
        result = await provisioner.provision_events(
            queue_name=QUEUE, topic_name=TOPIC, configuration_set=CONFIG_SET
        )

        updated = await provisioner.set_open_click_tracking(result, enabled=True)

        assert set(TRACKING_EVENT_TYPES) <= set(_matching_types(recorder.updated[0]))
        assert recorder.updated[0]["EventDestinationName"] == EVENT_DESTINATION_NAME
        assert updated.tracks_opens_and_clicks is True
        # Nothing else moved: the same queue and topic still carry the events.
        assert updated.queue_url == result.queue_url
        assert updated.topic_arn == result.topic_arn


async def test_turning_tracking_off_stops_requesting_it(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        provisioner, recorder = _provisioner(monkeypatch)
        result = await provisioner.provision_events(
            queue_name=QUEUE,
            topic_name=TOPIC,
            configuration_set=CONFIG_SET,
            track_opens_and_clicks=True,
        )

        updated = await provisioner.set_open_click_tracking(result, enabled=False)

        assert "OPEN" not in _matching_types(recorder.updated[0])
        assert updated.tracks_opens_and_clicks is False


# ----------------------------------------------------------------- canary ---


def test_moto_still_does_not_implement_event_destinations(aws_credentials: None) -> None:
    """A canary, not a specification.

    If moto implements these, this fails - and that failure is the signal to
    delete ``_RecordingSES`` and assert against moto's own state instead.
    Without it the workaround would quietly outlive its justification, exactly
    as the Phase 4 note warned.
    """
    with mock_aws():
        client = boto3.client("sesv2", region_name=REGION)
        client.create_configuration_set(ConfigurationSetName=CONFIG_SET)

        with pytest.raises(NotImplementedError):
            client.create_configuration_set_event_destination(
                ConfigurationSetName=CONFIG_SET,
                EventDestinationName=EVENT_DESTINATION_NAME,
                EventDestination={"Enabled": True, "MatchingEventTypes": ["DELIVERY"]},
            )
