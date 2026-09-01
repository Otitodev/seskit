"""Creating the AWS plumbing that carries events back (§15).

SES does not push delivery news anywhere by itself. Getting a bounce to reach
SESKit takes five resources wired in a particular order, across three services,
and a user should not have to learn any of that to find out that their mail did
not arrive - that is the whole product thesis, applied to the one place it is
most tempting to hand the work back with a documentation link.

What gets built::

    SQS queue          seskit-events        the worker polls this
    SNS topic          seskit-events        SES publishes here
    queue policy       topic -> queue       without it SNS may not deliver
    subscription       topic -> queue       and/or topic -> https endpoint
    configuration set  seskit               sends must name it
    event destination  set -> topic         which event types to publish

**Order is not arbitrary.** The queue must exist before its ARN can go in the
subscription; the queue policy must exist before the subscription, or SNS
delivers into a queue that refuses it and the messages are dropped silently;
the topic must exist before the event destination can point at it.

**Raw message delivery stays off.** With it on, SQS receives the bare event and
the SNS envelope is gone - and the envelope's ``MessageId`` is what
deduplication keys on, because the event body is byte-identical across
redeliveries. Turning raw delivery on would look like a tidy simplification and
would quietly break exactly-once.

**Everything here is idempotent.** ``CreateQueue`` and ``CreateTopic`` return
the existing resource; a configuration set that exists is caught and treated as
success. A user who clicks the button twice because nothing appeared to happen
gets one set of infrastructure, not two.

**moto 5.2.3, checked 2026-09-01:** SQS and SNS are fully implemented, and so
are SESv2 ``CreateConfigurationSet``/``DeleteConfigurationSet``. The four
``*ConfigurationSetEventDestination`` calls are **not** - they raise
``NotImplementedError``, the same gap Phase 4 found in ``GetAccount``. See
``tests/test_event_provisioning.py`` for how that is covered and for the canary
that will fail when moto catches up.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError
from seskit_core.logging import get_logger
from seskit_core.providers.types import EventInfrastructure

from seskit_provider_aws_ses.client import BOTO_CONFIG, build_session, call
from seskit_provider_aws_ses.errors import error_code, normalise_boto_error

logger = get_logger(__name__)

#: The one event destination on the configuration set. Named, because updating
#: the tracking toggle has to address it.
EVENT_DESTINATION_NAME = "seskit"

#: Always published. These are outcomes the user did not choose to observe -
#: they are what happened to mail they sent, and none of them changes the
#: message anybody receives.
BASE_EVENT_TYPES: tuple[str, ...] = (
    "SEND",
    "DELIVERY",
    "BOUNCE",
    "COMPLAINT",
    "REJECT",
    "RENDERING_FAILURE",
    "DELIVERY_DELAY",
)

#: Published only when a project opts in. Asking SES for these is what makes it
#: rewrite every link in the customer's mail and inject a tracking pixel, which
#: is a visible change to *their* product with privacy consequences. Confirmed
#: against the SES documentation rather than from memory: adding these to
#: MatchingEventTypes is the whole mechanism, and only a custom tracking domain
#: (§10, deferred to V1.1) needs anything further.
TRACKING_EVENT_TYPES: tuple[str, ...] = ("OPEN", "CLICK")

SQS_CREATE_ACTION = "sqs:CreateQueue"
SQS_ATTRIBUTES_ACTION = "sqs:GetQueueAttributes"
SQS_POLICY_ACTION = "sqs:SetQueueAttributes"
SQS_DELETE_ACTION = "sqs:DeleteQueue"
SNS_CREATE_ACTION = "sns:CreateTopic"
SNS_SUBSCRIBE_ACTION = "sns:Subscribe"
SNS_UNSUBSCRIBE_ACTION = "sns:Unsubscribe"
SNS_DELETE_ACTION = "sns:DeleteTopic"
SES_CREATE_SET_ACTION = "ses:CreateConfigurationSet"
SES_DELETE_SET_ACTION = "ses:DeleteConfigurationSet"
SES_EVENT_DESTINATION_ACTION = "ses:CreateConfigurationSetEventDestination"
SES_UPDATE_DESTINATION_ACTION = "ses:UpdateConfigurationSetEventDestination"
SES_DELETE_DESTINATION_ACTION = "ses:DeleteConfigurationSetEventDestination"

#: SES v2 documents ``AlreadyExistsException`` for a duplicate configuration
#: set; moto raises ``ConfigurationSetAlreadyExistsException``. Both are here
#: because a test that only passes against the mock proves nothing about the
#: real service, and vice versa.
_ALREADY_EXISTS = frozenset({"AlreadyExistsException", "ConfigurationSetAlreadyExistsException"})

#: Already gone is a success: the caller wanted it absent and it is.
_NOT_FOUND = frozenset(
    {
        "NotFoundException",
        "ConfigurationSetDoesNotExistException",
        "EventDestinationDoesNotExistException",
        "NotFound",
        "ResourceNotFoundException",
        "AWS.SimpleQueueService.NonExistentQueue",
        "InvalidParameterValue",
    }
)


def event_types(*, track_opens_and_clicks: bool) -> list[str]:
    """Which SES event types the destination should publish."""
    types = list(BASE_EVENT_TYPES)
    if track_opens_and_clicks:
        types.extend(TRACKING_EVENT_TYPES)
    return types


def queue_policy(*, queue_arn: str, topic_arn: str) -> str:
    """Let exactly one topic write to this queue.

    Scoped by ``SourceArn`` rather than allowing the SNS service at large.
    Without the condition any SNS topic in any account could post into this
    queue, and SESKit would happily record whatever bounces it was told about.
    """
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "seskit-events-from-sns",
                    "Effect": "Allow",
                    "Principal": {"Service": "sns.amazonaws.com"},
                    "Action": "sqs:SendMessage",
                    "Resource": queue_arn,
                    "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}},
                }
            ],
        }
    )


class SESEventProvisioner:
    """Builds and removes event infrastructure in one account and region.

    Constructed per request like :class:`~seskit_provider_aws_ses.SESProvider`,
    and for the same reasons: clients are cheap, region varies per project, and
    a cached client outlives the credentials it was built with.
    """

    def __init__(self, region: str) -> None:
        self.region = region
        self._session = build_session(region)

    # ------------------------------------------------------------- create ---

    async def provision_events(
        self,
        *,
        queue_name: str,
        topic_name: str,
        configuration_set: str,
        https_endpoint: str | None = None,
        track_opens_and_clicks: bool = False,
    ) -> EventInfrastructure:
        """Create everything, in the order the dependencies require."""
        topic_arn = await self._create_topic(topic_name)

        queue_url = queue_arn = subscription_arn = ""
        if queue_name:
            queue_url = await self._create_queue(queue_name)
            queue_arn = await self._queue_arn(queue_url)
            # Before the subscription, deliberately. A topic subscribed to a
            # queue it may not write to drops messages without erroring, which
            # is the hardest kind of failure to notice.
            await self._allow_topic_to_send(
                queue_url=queue_url, queue_arn=queue_arn, topic_arn=topic_arn
            )
            subscription_arn = await self._subscribe(topic_arn, protocol="sqs", endpoint=queue_arn)

        https_subscription_arn = ""
        if https_endpoint:
            # Returns "pending confirmation" until SES's own confirmation POST
            # is answered by the receiver, which is why the ARN is stored as
            # whatever came back rather than being validated here.
            https_subscription_arn = await self._subscribe(
                topic_arn, protocol="https", endpoint=https_endpoint
            )

        await self._create_configuration_set(configuration_set)
        await self._put_event_destination(
            configuration_set,
            topic_arn=topic_arn,
            track_opens_and_clicks=track_opens_and_clicks,
        )

        logger.info(
            "event_infrastructure_provisioned",
            region=self.region,
            configuration_set=configuration_set,
            has_queue=bool(queue_url),
            has_https=bool(https_subscription_arn),
            tracking=track_opens_and_clicks,
        )
        return EventInfrastructure(
            configuration_set=configuration_set,
            topic_arn=topic_arn,
            queue_url=queue_url,
            queue_arn=queue_arn,
            subscription_arn=subscription_arn,
            https_subscription_arn=https_subscription_arn,
            tracks_opens_and_clicks=track_opens_and_clicks,
        )

    # ------------------------------------------------------------- remove ---

    async def remove_events(self, infrastructure: EventInfrastructure) -> None:
        """Remove what was created, in reverse.

        Reverse order matters: SES must stop publishing before the topic goes,
        and the subscriptions must go before the queue, or SNS spends a while
        retrying into a queue that no longer exists.

        Each step tolerates "already gone" and none of them stops the others. A
        teardown that aborts halfway leaves resources nobody will ever
        identify again, which is worse than the error it reported.
        """
        if infrastructure.configuration_set:
            await self._delete_event_destination(infrastructure.configuration_set)
            await self._delete_configuration_set(infrastructure.configuration_set)

        for arn in (infrastructure.subscription_arn, infrastructure.https_subscription_arn):
            if arn:
                await self._unsubscribe(arn)

        if infrastructure.topic_arn:
            await self._delete_topic(infrastructure.topic_arn)

        if infrastructure.queue_url:
            await self._delete_queue(infrastructure.queue_url)

        logger.info("event_infrastructure_removed", region=self.region)

    # ------------------------------------------------------------ tracking ---

    async def set_open_click_tracking(
        self, infrastructure: EventInfrastructure, *, enabled: bool
    ) -> EventInfrastructure:
        """Add or remove OPEN and CLICK on the existing event destination."""
        await self._put_event_destination(
            infrastructure.configuration_set,
            topic_arn=infrastructure.topic_arn,
            track_opens_and_clicks=enabled,
            update=True,
        )
        logger.info(
            "event_tracking_changed",
            configuration_set=infrastructure.configuration_set,
            enabled=enabled,
        )
        return EventInfrastructure(
            configuration_set=infrastructure.configuration_set,
            topic_arn=infrastructure.topic_arn,
            queue_url=infrastructure.queue_url,
            queue_arn=infrastructure.queue_arn,
            subscription_arn=infrastructure.subscription_arn,
            https_subscription_arn=infrastructure.https_subscription_arn,
            tracks_opens_and_clicks=enabled,
        )

    # ------------------------------------------------------------ internal ---

    def _sqs(self) -> Any:
        return self._session.client("sqs", config=BOTO_CONFIG)

    def _sns(self) -> Any:
        return self._session.client("sns", config=BOTO_CONFIG)

    def _ses(self) -> Any:
        return self._session.client("sesv2", config=BOTO_CONFIG)

    async def _create_queue(self, name: str) -> str:
        try:
            response = await call(self._sqs().create_queue, QueueName=name)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SQS_CREATE_ACTION) from exc
        return str(response["QueueUrl"])

    async def _queue_arn(self, queue_url: str) -> str:
        try:
            response = await call(
                self._sqs().get_queue_attributes,
                QueueUrl=queue_url,
                AttributeNames=["QueueArn"],
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=SQS_ATTRIBUTES_ACTION) from exc
        return str(response["Attributes"]["QueueArn"])

    async def _allow_topic_to_send(self, *, queue_url: str, queue_arn: str, topic_arn: str) -> None:
        try:
            await call(
                self._sqs().set_queue_attributes,
                QueueUrl=queue_url,
                Attributes={"Policy": queue_policy(queue_arn=queue_arn, topic_arn=topic_arn)},
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=SQS_POLICY_ACTION) from exc

    async def _create_topic(self, name: str) -> str:
        try:
            response = await call(self._sns().create_topic, Name=name)
        except Exception as exc:
            raise normalise_boto_error(exc, action=SNS_CREATE_ACTION) from exc
        return str(response["TopicArn"])

    async def _subscribe(self, topic_arn: str, *, protocol: str, endpoint: str) -> str:
        try:
            response = await call(
                self._sns().subscribe,
                TopicArn=topic_arn,
                Protocol=protocol,
                Endpoint=endpoint,
                # Off, and this is load-bearing. Raw delivery strips the SNS
                # envelope, and its MessageId is what deduplication keys on -
                # the event body is identical across redeliveries.
                Attributes={"RawMessageDelivery": "false"},
                ReturnSubscriptionArn=True,
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=SNS_SUBSCRIBE_ACTION) from exc
        return str(response["SubscriptionArn"])

    async def _create_configuration_set(self, name: str) -> None:
        try:
            await call(self._ses().create_configuration_set, ConfigurationSetName=name)
        except ClientError as exc:
            if error_code(exc) in _ALREADY_EXISTS:
                return
            raise normalise_boto_error(exc, action=SES_CREATE_SET_ACTION) from exc
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_CREATE_SET_ACTION) from exc

    async def _put_event_destination(
        self,
        configuration_set: str,
        *,
        topic_arn: str,
        track_opens_and_clicks: bool,
        update: bool = False,
    ) -> None:
        """Create the destination, or update it if it is already there.

        The create/update distinction is SES's, not one worth exposing: the
        caller wants the destination to end up in a particular state, so a
        create that collides falls through to an update.
        """
        destination = {
            "Enabled": True,
            "MatchingEventTypes": event_types(track_opens_and_clicks=track_opens_and_clicks),
            "SnsDestination": {"TopicArn": topic_arn},
        }
        client = self._ses()

        if not update:
            try:
                await call(
                    client.create_configuration_set_event_destination,
                    ConfigurationSetName=configuration_set,
                    EventDestinationName=EVENT_DESTINATION_NAME,
                    EventDestination=destination,
                )
                return
            except ClientError as exc:
                if error_code(exc) not in _ALREADY_EXISTS:
                    raise normalise_boto_error(exc, action=SES_EVENT_DESTINATION_ACTION) from exc
            except Exception as exc:
                raise normalise_boto_error(exc, action=SES_EVENT_DESTINATION_ACTION) from exc

        try:
            await call(
                client.update_configuration_set_event_destination,
                ConfigurationSetName=configuration_set,
                EventDestinationName=EVENT_DESTINATION_NAME,
                EventDestination=destination,
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=SES_UPDATE_DESTINATION_ACTION) from exc

    async def _delete_event_destination(self, configuration_set: str) -> None:
        await self._tolerate_missing(
            self._ses().delete_configuration_set_event_destination,
            action=SES_DELETE_DESTINATION_ACTION,
            ConfigurationSetName=configuration_set,
            EventDestinationName=EVENT_DESTINATION_NAME,
        )

    async def _delete_configuration_set(self, name: str) -> None:
        await self._tolerate_missing(
            self._ses().delete_configuration_set,
            action=SES_DELETE_SET_ACTION,
            ConfigurationSetName=name,
        )

    async def _unsubscribe(self, subscription_arn: str) -> None:
        if not subscription_arn.startswith("arn:"):
            # SNS returns "PendingConfirmation" instead of an ARN for an
            # unconfirmed HTTPS subscription. There is nothing to unsubscribe
            # yet, and passing that string back would be an error, not a no-op.
            return
        await self._tolerate_missing(
            self._sns().unsubscribe,
            action=SNS_UNSUBSCRIBE_ACTION,
            SubscriptionArn=subscription_arn,
        )

    async def _delete_topic(self, topic_arn: str) -> None:
        await self._tolerate_missing(
            self._sns().delete_topic, action=SNS_DELETE_ACTION, TopicArn=topic_arn
        )

    async def _delete_queue(self, queue_url: str) -> None:
        await self._tolerate_missing(
            self._sqs().delete_queue, action=SQS_DELETE_ACTION, QueueUrl=queue_url
        )

    async def _tolerate_missing(self, func: Any, *, action: str, **kwargs: Any) -> None:
        """Run one teardown step, treating "already gone" as done.

        Logged rather than raised for any other failure too. Teardown removes
        several things and the caller has already decided they should go; one
        step failing must not strand the rest, and the user needs to be told
        what is left rather than handed a traceback mid-way.
        """
        try:
            await call(func, **kwargs)
        except ClientError as exc:
            if error_code(exc) in _NOT_FOUND:
                return
            logger.warning("event_teardown_step_failed", action=action, code=error_code(exc))
        except Exception:
            logger.warning("event_teardown_step_failed", action=action)
