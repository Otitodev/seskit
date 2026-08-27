"""Amazon SES provider for SESKit (§8, §26).

Provider-specific logic stays inside this package and never leaks into the API
or core packages (§32.8). What crosses the boundary is core's vocabulary: the
dataclasses in ``seskit_core.providers.types`` and, on failure, ``APIError``.
"""

from seskit_provider_aws_ses.errors import NO_CREDENTIALS_MESSAGE, normalise_boto_error
from seskit_provider_aws_ses.provider import SESProvider
from seskit_provider_aws_ses.regions import SES_REGION_CODES, SES_REGIONS, is_known_region

__all__ = [
    "NO_CREDENTIALS_MESSAGE",
    "SES_REGIONS",
    "SES_REGION_CODES",
    "SESProvider",
    "is_known_region",
    "normalise_boto_error",
]
