"""Reading notifications off an SQS queue (§15).

The default transport, because it works everywhere §9 says SESKit has to run.
There is no inbound port to open, no public hostname, and no certificate: a
laptop behind NAT polls the same way a server does. It is also authenticated by
IAM rather than by signature, which removes three of the four security
requirements in ``docs/prior-art.md`` by construction - there is no endpoint to
forge a request at, no signature to verify, and no ``SubscribeURL`` to be
tricked into fetching.

**Long polling, not a busy loop.** ``WaitTimeSeconds`` holds the request open
until a message arrives or the wait expires. Short polling would mean a choice
between latency and a request every few hundred milliseconds forever, and SQS
bills per request.

**Nothing is deleted here except on instruction.** A message stays invisible for
the visibility timeout and returns if it is not deleted, which is what makes a
crash mid-ingest lose nothing. Deleting on receipt would be simpler and would
turn every failure into a silently lost delivery event.
"""

from __future__ import annotations

from typing import Any

from seskit_core.logging import get_logger
from seskit_core.providers.types import QueuedNotification

from seskit_provider_aws_ses.client import BOTO_CONFIG, build_session, call
from seskit_provider_aws_ses.errors import normalise_boto_error

logger = get_logger(__name__)

SQS_RECEIVE_ACTION = "sqs:ReceiveMessage"
SQS_DELETE_ACTION = "sqs:DeleteMessage"

#: SQS accepts at most ten messages per receive and at most twenty seconds of
#: long polling. Named rather than inlined so a configuration value that
#: exceeds them is clamped instead of rejected by AWS at runtime.
MAX_MESSAGES_PER_RECEIVE = 10
MAX_WAIT_SECONDS = 20


class SQSNotificationQueue:
    """One SQS queue, read one batch at a time."""

    def __init__(self, region: str, queue_url: str) -> None:
        self.region = region
        self.queue_url = queue_url
        self._session = build_session(region)

    def _client(self) -> Any:
        return self._session.client("sqs", config=BOTO_CONFIG)

    async def receive(
        self,
        *,
        max_messages: int = MAX_MESSAGES_PER_RECEIVE,
        wait_seconds: int = MAX_WAIT_SECONDS,
        visibility_timeout: int = 60,
    ) -> list[QueuedNotification]:
        """Take up to a batch off the queue, waiting if it is empty.

        The visibility timeout is how long this consumer has to finish before
        the message comes back. It must comfortably exceed the time to record
        one event, or a slow database turns into duplicate processing - which
        the unique constraint on ``provider_event_id`` then absorbs, but only
        because it is there.
        """
        try:
            response = await call(
                self._client().receive_message,
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=min(max_messages, MAX_MESSAGES_PER_RECEIVE),
                WaitTimeSeconds=min(wait_seconds, MAX_WAIT_SECONDS),
                VisibilityTimeout=visibility_timeout,
            )
        except Exception as exc:
            raise normalise_boto_error(exc, action=SQS_RECEIVE_ACTION) from exc

        return [
            QueuedNotification(
                receipt=str(message.get("ReceiptHandle", "")),
                body=str(message.get("Body", "")),
                queue_message_id=str(message.get("MessageId", "")),
            )
            for message in response.get("Messages", [])
        ]

    async def delete(self, notification: QueuedNotification) -> None:
        """Acknowledge one message.

        Called only once the event is settled - recorded, a known duplicate, or
        something no amount of retrying will fix. Anything else is left to
        reappear.
        """
        try:
            await call(
                self._client().delete_message,
                QueueUrl=self.queue_url,
                ReceiptHandle=notification.receipt,
            )
        except Exception as exc:
            # Not fatal: the message will simply be delivered again, and
            # deduplication makes that harmless. Raising here would abandon a
            # batch that was otherwise processed successfully.
            logger.warning(
                "event_ack_failed",
                queue_url=self.queue_url,
                error=type(exc).__name__,
            )
