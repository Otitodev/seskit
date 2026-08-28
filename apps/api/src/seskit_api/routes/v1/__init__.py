"""The public API (§23).

Everything under ``/v1`` is authenticated by an API key, versioned, and part of
the OpenAPI document - unlike the dashboard routes, which are excluded from it
because the schema is for customers.

The prefix is ``/v1`` from the first endpoint. Adding a version later means
either breaking every caller or serving two shapes for ever.
"""

from fastapi import APIRouter

from seskit_api.routes.v1 import api_keys, domains, emails

router = APIRouter(prefix="/v1")
router.include_router(api_keys.router)
router.include_router(domains.router)
router.include_router(emails.router)

__all__ = ["router"]
