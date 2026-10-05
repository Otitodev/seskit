"""Amazon SES provider for SESKit (§8, §26).

Provider-specific logic stays inside this package and never leaks into the API
or core packages (§32.8). What crosses the boundary is core's vocabulary: the
dataclasses in ``seskit_core.providers.types`` and, on failure, ``APIError``.
"""

from seskit_provider_aws_ses.errors import NO_CREDENTIALS_MESSAGE, normalise_boto_error
from seskit_provider_aws_ses.inbound import (
    DEFAULT_RULE_SET,
    EXPIRED_MESSAGE,
    MAX_MESSAGE_BYTES,
    RULE_NAME_PREFIX,
    S3InboundStore,
    SESInboundProvisioner,
    bucket_policy,
    receipt_rule,
)
from seskit_provider_aws_ses.provider import SESProvider
from seskit_provider_aws_ses.provisioning import (
    BASE_EVENT_TYPES,
    EVENT_DESTINATION_NAME,
    TRACKING_EVENT_TYPES,
    SESEventProvisioner,
    event_types,
    queue_policy,
)
from seskit_provider_aws_ses.regions import (
    RECEIVING_REGION_CODES,
    SES_REGION_CODES,
    SES_REGIONS,
    is_known_region,
    supports_receiving,
)
from seskit_provider_aws_ses.sns_signature import (
    AWS_SNS_HOST,
    SignatureError,
    assert_aws_url,
    canonical_string,
    confirm_subscription,
    verify,
)
from seskit_provider_aws_ses.sqs import SQSNotificationQueue

__all__ = [
    "AWS_SNS_HOST",
    "BASE_EVENT_TYPES",
    "DEFAULT_RULE_SET",
    "EVENT_DESTINATION_NAME",
    "EXPIRED_MESSAGE",
    "MAX_MESSAGE_BYTES",
    "NO_CREDENTIALS_MESSAGE",
    "RECEIVING_REGION_CODES",
    "RULE_NAME_PREFIX",
    "SES_REGIONS",
    "SES_REGION_CODES",
    "TRACKING_EVENT_TYPES",
    "S3InboundStore",
    "SESEventProvisioner",
    "SESInboundProvisioner",
    "SESProvider",
    "SQSNotificationQueue",
    "SignatureError",
    "assert_aws_url",
    "bucket_policy",
    "canonical_string",
    "confirm_subscription",
    "event_types",
    "is_known_region",
    "normalise_boto_error",
    "queue_policy",
    "receipt_rule",
    "supports_receiving",
    "verify",
]
