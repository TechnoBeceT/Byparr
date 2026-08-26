"""Browser resource creation and deterministic cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

from invisible_playwright.async_api import InvisiblePlaywright
from playwright.async_api import Browser, BrowserContext, Page

from src.consts import BROWSER_LOCALE
from src.proxy import ProxySettings


class BrowserDepClass(NamedTuple):
    """The page and context exposed to request handlers."""

    page: Page
    context: BrowserContext


@dataclass
class BrowserResource:
    """One browser context and page, with ownership of their enclosing scope."""

    page: Page
    context: BrowserContext
    _scope: AbstractAsyncContextManager[Browser] = field(repr=False)
    _close_task: asyncio.Task[BaseException | None] | None = field(
        default=None, init=False, repr=False
    )

    async def close(self) -> None:
        """Await the one shared cleanup task, replaying any cleanup error."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        error = await asyncio.shield(self._close_task)
        if error is not None:
            raise error

    async def _close_owned(self) -> BaseException | None:
        """Close once and return the first cleanup error without failing the task."""
        try:
            await self.context.close()
        except BaseException as error:
            with suppress(BaseException):
                await self._scope.__aexit__(None, None, None)
            return error
        try:
            await self._scope.__aexit__(None, None, None)
        except BaseException as error:
            return error
        return None


class BrowserFactory:
    """Open independent InvisiblePlaywright resources for selected proxies."""

    def __init__(
        self,
        playwright_factory: Callable[
            ..., AbstractAsyncContextManager[Any]
        ] = InvisiblePlaywright,
    ) -> None:
        """Configure the InvisiblePlaywright context manager constructor."""
        self._playwright_factory = playwright_factory

    async def open(self, proxy: ProxySettings) -> BrowserResource:
        """Enter a browser scope and create a context and page within it."""
        scope = self._playwright_factory(
            headless=True,
            proxy=proxy.as_playwright_proxy(),
            humanize=True,
            locale=BROWSER_LOCALE or "auto",
            extra_prefs={
                "devtools.jsonview.enabled": False,
                "browser.tabs.remote.useCrossOriginOpenerPolicy": False,
                "browser.tabs.remote.useCrossOriginEmbedderPolicy": False,
            },
        )
        try:
            browser = cast("Browser", await scope.__aenter__())
        except BaseException as error:
            await self._close_after_open_failure(scope, None, error)
            raise
        context: BrowserContext | None = None
        try:
            context = await browser.new_context()
            page = await context.new_page()
        except BaseException as error:
            await self._close_after_open_failure(scope, context, error)
            raise
        return BrowserResource(page=page, context=context, _scope=scope)

    @staticmethod
    async def _close_after_open_failure(
        scope: AbstractAsyncContextManager[Any],
        context: BrowserContext | None,
        error: BaseException,
    ) -> None:
        """Release partially opened resources without hiding the original failure."""
        with suppress(BaseException):
            if context is not None:
                await context.close()
        with suppress(BaseException):
            await scope.__aexit__(type(error), error, error.__traceback__)
