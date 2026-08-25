"""SESKit Python SDK.

Implemented in Phase 10 (see SESKit_MVP.md §31). The SDK is a thin client over
the HTTP API - business logic lives in the API, never duplicated here (§13).

Target surface:

    from seskit import SesKit

    client = SesKit(api_key="sk_live_xxx")
    client.emails.send(...)
    client.emails.get(...)
    client.emails.list()
"""

__all__: list[str] = []
