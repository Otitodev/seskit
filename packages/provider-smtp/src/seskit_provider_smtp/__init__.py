"""SMTP provider for SESKit (§25, §26).

The local-development backend, and the reason a first send does not need an AWS
account. Provider-specific logic stays inside this package; what crosses the
boundary is core's vocabulary (§32.8).
"""

from seskit_provider_smtp.provider import SMTPProvider, SMTPSettings

__all__ = ["SMTPProvider", "SMTPSettings"]
