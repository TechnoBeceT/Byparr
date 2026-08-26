import logging
import time
from collections.abc import AsyncGenerator
from typing import Annotated, NamedTuple

from fastapi import Header
from playwright.async_api import BrowserContext, Page
from pydantic import BaseModel, Field

from src.browser import BrowserFactory
from src.consts import (
    LOG_LEVEL,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
)
from src.proxy import resolve_proxy_settings

solver_logger = logging.getLogger("playwright_captcha")
solver_logger.handlers.clear()
if LOG_LEVEL == logging.DEBUG:
    solver_logger.addHandler(logging.StreamHandler())
    solver_logger.setLevel(LOG_LEVEL)
else:
    solver_logger.handlers.append(logging.NullHandler())

logger = logging.getLogger("uvicorn.error")
logger.setLevel(LOG_LEVEL)
if len(logger.handlers) == 0:
    logger.addHandler(logging.StreamHandler())


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


class BrowserDepClass(NamedTuple):
    page: Page
    context: BrowserContext


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
        yield BrowserDepClass(resource.page, resource.context)
    finally:
        await resource.close()
