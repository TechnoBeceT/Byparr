import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from src.browser import BrowserDepClass, BrowserFactory, is_fatal_browser_error
from src.consts import (
    LOG_LEVEL,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
)
from src.models import LinkRequest
from src.proxy import resolve_proxy_settings
from src.session_key import build_session_key, safe_site_label
from src.sessions import SessionCapacityError, SessionManager

SESSION_CAPACITY_MESSAGE = "Browser session capacity is unavailable"
SESSION_LIFESPAN_REQUIRED_MESSAGE = (
    "named browser sessions require application lifespan"
)

solver_logger = logging.getLogger("playwright_captcha")
solver_logger.handlers.clear()
solver_log_sink = logging.NullHandler()
solver_logger.addHandler(solver_log_sink)
solver_logger.propagate = False
solver_logger.disabled = True
solver_logger.setLevel(logging.CRITICAL + 1)

logger = logging.getLogger("uvicorn.error")
logger.setLevel(LOG_LEVEL)
if len(logger.handlers) == 0:
    logger.addHandler(logging.StreamHandler())


def log_browser_error(
    event: str,
    url: str,
    error: BaseException,
    *,
    level: int = logging.ERROR,
    outcome: str | None = None,
) -> None:
    """Render browser diagnostics without serializing URL or exception secrets."""
    fields = {
        "site": safe_site_label(url),
        "error_type": type(error).__name__,
    }
    if outcome is not None:
        fields["outcome"] = outcome
    rendered = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.log(level, f"{event} {rendered}", extra=fields)


class TimeoutTimer(BaseModel):
    duration: int  # in seconds
    start_time: float = Field(default_factory=time.perf_counter)

    def remaining(self) -> float:
        """Get remaining time in seconds."""
        return max(0, self.duration - (time.perf_counter() - self.start_time))


MIN_WAIT_MS = 1.0


def remaining_ms(timer: TimeoutTimer) -> float:
    """Milliseconds left, never 0 - Playwright reads that as no timeout at all."""
    return max(MIN_WAIT_MS, timer.remaining() * 1000)


async def get_browser(
    x_proxy_server: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Server",
            description="Override proxy server for this request in protocol://host:port format.",
        ),
    ] = None,
    x_proxy_username: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Username",
        ),
    ] = None,
    x_proxy_password: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Password",
        ),
    ] = None,
) -> AsyncGenerator[BrowserDepClass]:
    """Open and dispose a browser resource for one request."""
    proxy = resolve_proxy_settings(
        header_server=x_proxy_server,
        header_username=x_proxy_username,
        header_password=x_proxy_password,
        environment_server=PROXY_SERVER,
        environment_username=PROXY_USERNAME,
        environment_password=PROXY_PASSWORD,
    )
    resource = await BrowserFactory().open(proxy)
    try:
        yield BrowserDepClass(resource.page, resource.context, resource.solver)
    finally:
        await resource.close()


@asynccontextmanager
async def get_request_browser(
    request: LinkRequest,
    manager: SessionManager | None,
    *,
    x_proxy_server: str | None = None,
    x_proxy_username: str | None = None,
    x_proxy_password: str | None = None,
    generation: str | None = None,
) -> AsyncGenerator[BrowserDepClass]:
    """Acquire a disposable or retained browser for a FlareSolverr request."""
    proxy = resolve_proxy_settings(
        header_server=x_proxy_server,
        header_username=x_proxy_username,
        header_password=x_proxy_password,
        environment_server=PROXY_SERVER,
        environment_username=PROXY_USERNAME,
        environment_password=PROXY_PASSWORD,
    )
    key = build_session_key(request.session, request.url, proxy)
    if key is None:
        resource = await BrowserFactory().open(proxy)
        try:
            yield BrowserDepClass(resource.page, resource.context, resource.solver)
        finally:
            await resource.close()
        return
    if manager is None:
        raise RuntimeError(SESSION_LIFESPAN_REQUIRED_MESSAGE)
    try:
        async with manager.acquire(key, proxy, generation=generation) as browser:
            try:
                yield browser
            except BaseException as error:
                if is_fatal_browser_error(error):
                    await manager.invalidate(key)
                raise
    except SessionCapacityError as error:
        raise HTTPException(status_code=503, detail=SESSION_CAPACITY_MESSAGE) from error
