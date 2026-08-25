"""Request ID middleware.

§21 requires every email request to carry a request ID through the logs. Doing
it here means every log line emitted during a request gets the ID for free,
rather than each call site having to thread it through.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from seskit_core.logging import get_logger, request_id_var
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request ID, bind it for logging, and echo it in the response.

    An inbound ``X-Request-ID`` is honoured so a request can be traced across a
    proxy; otherwise one is generated.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Log with the request ID still bound, then let the exception
            # handlers turn it into a response.
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            request_id_var.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id

        # Health probes run constantly; logging them buries real traffic.
        if not request.url.path.startswith(("/healthz", "/readyz", "/static")):
            logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        return response
