import logging
import time
from collections.abc import Mapping
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
        _log_solve(logging.INFO, "solve_start", context)
        try:
            response = await call_next(request)
        except BaseException:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            _log_solve(
                logging.WARNING,
                "solve_finish",
                context,
                outcome="exception",
                duration_ms=duration_ms,
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
        level = logging.INFO if outcome == "success" else logging.WARNING
        _log_solve(
            level,
            "solve_finish",
            context,
            outcome=outcome,
            duration_ms=duration_ms,
        )

        return response


def _log_solve(
    level: int,
    event: str,
    context: Mapping[str, object],
    *,
    outcome: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Emit safe solve fields in both the record and shipped text output."""
    fields: dict[str, object] = {"event": event, **context}
    if outcome is not None:
        fields["outcome"] = outcome
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    rendered = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.log(level, f"browser_solve {rendered}", extra=fields)
