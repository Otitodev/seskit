"""``POST /v1/events/ses`` - receiving SNS notifications over HTTPS (§15).

The second transport, for deployments that have a public address and would
rather be pushed to than poll. SQS remains the default because it works
everywhere §9 says SESKit has to run; this exists because polling is not free
and a server with a hostname should not have to.

**This endpoint is unauthenticated by design and cannot be otherwise.** SNS has
no credential to present. Everything that stands between it and a stranger
posting fabricated bounces is the signature check, which is why the four
requirements drawn from `docs/prior-art.md` all land here:

1. The RSA signature is verified over SNS's canonical string. Checking
   ``TopicArn`` instead - as the project recorded there does - checks a field
   in the request body against a value that is not a secret.
2. ``SigningCertURL`` and ``SubscribeURL`` are validated *before* being
   fetched. Both are attacker-supplied; fetching first would make this an
   SSRF gadget pointed at whatever the instance can reach.
3. Deduplication is on the SNS ``MessageId``, which the ingest path already
   enforces with a unique constraint.
4. **A failure answers non-2xx.** SNS retries on anything else, and that is
   the behaviour we want: answering 200 to a request we failed to process, as
   the project in prior-art does on parse errors, drops the event for good.

The endpoint is only mounted when ``EVENT_INGESTION`` asks for it. An endpoint
that exists but is not subscribed to is only an attack surface.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Request, Response, status
from seskit_core.config import EVENT_HTTPS_PATH, Settings
from seskit_core.db import get_session
from seskit_core.events import MalformedEnvelope, Outcome, ingest_event, unwrap
from seskit_core.logging import get_logger
from seskit_core.services import pending_delivery_ids
from seskit_provider_aws_ses import SignatureError, confirm_subscription, verify
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import get_app_settings
from seskit_api.queue import get_queue

logger = get_logger(__name__)

router = APIRouter(tags=["events"], include_in_schema=False)

#: SNS sends a body far smaller than this. A ceiling stops an unauthenticated
#: endpoint being used to make the process read an arbitrary amount into memory.
MAX_BODY_BYTES = 256 * 1024

#: The path this router serves, taken from the same constant provisioning
#: subscribes SNS to. The router already carries the ``/v1`` prefix.
EVENT_PATH = EVENT_HTTPS_PATH.removeprefix("/v1")


@router.post(
    EVENT_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Receive an SES event notification from SNS",
)
async def receive_ses_event(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    queue: Annotated[ArqRedis, Depends(get_queue)],
) -> Response:
    """Record one notification, or refuse it.

    The status code is the whole protocol here: 204 means settled and SNS can
    forget it, 403 means it was not from SNS, and 500 means try again. There is
    no body worth sending - nothing reads it, and saying *why* a request was
    refused tells whoever is probing which half to work on next.
    """
    if not settings.receives_https:
        # Not merely unconfigured: an endpoint that is not subscribed to has no
        # legitimate traffic, so this is the honest answer and it keeps the
        # surface closed by default.
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

    try:
        envelope = unwrap(raw)
    except MalformedEnvelope:
        # Settled: a body that is not an SNS message will not become one on the
        # next attempt, and asking SNS to retry it forever helps nobody.
        logger.info("sns_body_unreadable")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    body: dict[str, Any] = _parsed(raw)

    try:
        await verify(body)
    except SignatureError:
        # Deliberately indistinguishable from every other signature failure.
        logger.warning("sns_signature_rejected", message_type=envelope.message_type)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    if envelope.is_subscription_confirmation:
        return await _confirm(envelope.subscribe_url)

    if not envelope.is_notification:
        # An unsubscribe confirmation, most likely. Signed and genuine, but
        # there is nothing to record.
        logger.info("sns_message_ignored", message_type=envelope.message_type)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if not envelope.event:
        logger.warning("sns_payload_empty", sns_message_id=envelope.message_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    outcome, event = await ingest_event(
        db,
        envelope.event,
        # The envelope's id: the event body is identical across redeliveries,
        # so nothing inside it could tell one from another.
        provider_event_id=envelope.message_id or None,
    )
    # Read before the commit, so the ids are available afterwards.
    delivery_ids = await pending_delivery_ids(db, event.id) if event is not None else []
    await db.commit()

    for delivery_id in delivery_ids:
        # Latency only - the delivery row is what makes the webhook durable, so
        # a failure to enqueue costs seconds rather than the webhook itself.
        await queue.enqueue_job("deliver_webhook", delivery_id)

    if outcome is Outcome.RECORDED:
        logger.info("sns_event_recorded", sns_message_id=envelope.message_id)

    # Everything ingest_event returns is settled; a transient failure raises
    # instead and becomes a 500, which is exactly when SNS should try again.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _confirm(subscribe_url: str) -> Response:
    """Answer the SNS handshake.

    The URL is validated against the AWS SNS host inside
    ``confirm_subscription`` before any request is made - a signed message is
    not a licence to fetch whatever URL it names, and the signature check that
    just passed does not remove the need for the second lock.
    """
    try:
        await confirm_subscription(subscribe_url)
    except SignatureError:
        logger.warning("sns_confirmation_refused")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parsed(raw: bytes) -> dict[str, Any]:
    """The body as a dict, for signature verification.

    Parsed from the raw bytes rather than taken from the envelope, because the
    signature covers the fields as sent - including ones the envelope has no
    use for, such as ``Token`` and ``Timestamp``.
    """
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}
