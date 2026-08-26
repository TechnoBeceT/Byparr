from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from src.browser import BrowserFactory
from src.consts import HOST, LOG_LEVEL, PORT, VERSION
from src.endpoints import health_check, router
from src.middlewares import LogRequest
from src.owui import router as owui_router
from src.sessions import SessionManager
from src.utils import get_browser, logger

logger.info("Using version %s", VERSION)
logger.info("Log level set to %s", logging.getLevelName(LOG_LEVEL))

_IDLE_EXPIRY_INTERVAL_SECONDS = 60


async def expire_idle_sessions(manager: SessionManager) -> None:
    """Periodically retire idle retained browsers until application shutdown."""
    while True:
        await asyncio.sleep(_IDLE_EXPIRY_INTERVAL_SECONDS)
        try:
            await manager.expire_idle()
        except Exception:
            logger.exception("Unable to expire idle browser sessions")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the bounded retained-session manager for the application lifetime."""
    manager = SessionManager(BrowserFactory())
    application.state.session_manager = manager
    expiry_task = asyncio.create_task(expire_idle_sessions(manager))
    try:
        yield
    finally:
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task
        await manager.close()


app = FastAPI(
    debug=LOG_LEVEL == logging.DEBUG,
    log_level=LOG_LEVEL,
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware)
app.add_middleware(LogRequest)

app.include_router(router=router)
app.include_router(router=owui_router)


async def init():
    """Initialize the application."""
    async for browser in get_browser():
        await health_check(browser)


if __name__ == "__main__":
    # Check for --init flag to run the app in development mode
    if "--init" in sys.argv:
        logger.info("Running initialization script...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init())
        logger.info("Initialization complete.")
    else:
        uvicorn.run(app, host=HOST, port=PORT)
