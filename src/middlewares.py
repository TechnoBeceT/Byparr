import time
from http import HTTPStatus
from json import JSONDecodeError

from fastapi import Request
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.models import LinkRequest
from src.session_key import safe_site_label
from src.utils import logger


class LogRequest(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        """Log requests."""
        if request.url.path != "/v1" or request.method != "POST":
            return await call_next(request)

        start_time = time.perf_counter()
        try:
            request_body = LinkRequest.model_validate(await request.json())
        except JSONDecodeError, UnicodeDecodeError, ValidationError:
            return await call_next(request)
        context = {
            "site": safe_site_label(request_body.url),
            "timeout_seconds": request_body.max_timeout,
            "session_mode": "retained" if request_body.session else "disposable",
        }
        logger.info("browser_solve", extra={"event": "solve_start", **context})
        try:
            response = await call_next(request)
        except BaseException:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.warning(
                "browser_solve",
                extra={
                    "event": "solve_finish",
                    "outcome": "exception",
                    "duration_ms": duration_ms,
                    **context,
                },
            )
            raise

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        outcome = (
            "success"
            if response.status_code == HTTPStatus.OK
            else "timeout"
            if response.status_code == HTTPStatus.REQUEST_TIMEOUT
            else "failure"
        )
        log = logger.info if outcome == "success" else logger.warning
        log(
            "browser_solve",
            extra={
                "event": "solve_finish",
                "outcome": outcome,
                "duration_ms": duration_ms,
                **context,
            },
        )

        return response
