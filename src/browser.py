"""Browser resource creation and deterministic cleanup."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from typing import Any, cast

from invisible_playwright.async_api import InvisiblePlaywright
from playwright.async_api import Browser, BrowserContext, Page

from src.consts import BROWSER_LOCALE
from src.proxy import ProxySettings


@dataclass
class BrowserResource:
    """One browser context and page, with ownership of their enclosing scope."""

    page: Page
    context: BrowserContext
    _scope: AbstractAsyncContextManager[Browser] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        """Close this context and exit its InvisiblePlaywright scope once."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.context.close()
        finally:
            await self._scope.__aexit__(None, None, None)


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
        browser = cast("Browser", await scope.__aenter__())
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
