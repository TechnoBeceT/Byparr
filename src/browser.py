"""Browser resource creation and deterministic cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, cast

from camoufox import AsyncCamoufox
from playwright._impl._errors import is_target_closed_error
from playwright.async_api import Browser, BrowserContext, Page
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType

from src.consts import ADDON_PATH, BROWSER_LOCALE, MAX_ATTEMPTS
from src.proxy import ProxySettings


class BrowserResourceUnusableError(RuntimeError):
    """Signal that request-local browser state could not be restored safely."""


class FatalSolverBrowserError(BaseException):
    """Carry fatal browser closure through a dependency's Exception retry loop."""


def is_fatal_browser_error(error: BaseException) -> bool:
    """Classify Playwright's closed page, context, or browser failures."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BrowserResourceUnusableError):
            return True
        if isinstance(current, Exception) and is_target_closed_error(current):
            return True
        current = current.__cause__ or current.__context__
    return False


class ManagedClickSolver(ClickSolver):
    """Preserve ordinary retries while surfacing fatal browser closure promptly."""

    async def _solve_captcha_once(
        self,
        captcha_container: object,
        captcha_type: CaptchaType,
        **kwargs: object,
    ) -> None:
        try:
            await super()._solve_captcha_once(
                captcha_container, captcha_type, **kwargs
            )
        except Exception as error:
            if is_fatal_browser_error(error):
                raise FatalSolverBrowserError from error
            raise


class BrowserDepClass(NamedTuple):
    """The page and context exposed to request handlers."""

    page: Page
    context: BrowserContext
    solver: ClickSolver


class ManagedBrowserResource(Protocol):
    """The lifecycle surface retained by the session manager."""

    @property
    def page(self) -> object:
        """Return the resource's page handle."""
        ...

    @property
    def context(self) -> object:
        """Return the resource's context handle."""
        ...

    @property
    def solver(self) -> object:
        """Return the resource's page-bound challenge solver."""
        ...

    async def close(self) -> None:
        """Close every resource owned by this handle."""
        ...


class BrowserFactoryProtocol(Protocol):
    """Create one manager-owned browser resource for a selected proxy."""

    async def open(self, proxy: ProxySettings) -> ManagedBrowserResource:
        """Open a resource."""
        ...


@dataclass
class BrowserResource:
    """One browser context and page, with ownership of their enclosing scope."""

    page: Page
    context: BrowserContext
    solver: ClickSolver
    _scope: AbstractAsyncContextManager[Browser] = field(repr=False)
    _solver_scope: AbstractAsyncContextManager[ClickSolver] = field(repr=False)
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
        first_error: BaseException | None = None
        try:
            await self._solver_scope.__aexit__(None, None, None)
        except BaseException as error:
            first_error = error
        try:
            await self.context.close()
        except BaseException as error:
            first_error = first_error or error
        try:
            await self._scope.__aexit__(None, None, None)
        except BaseException as error:
            first_error = first_error or error
        return first_error


class BrowserFactory:
    """Open independent Camoufox resources for selected proxies."""

    def __init__(
        self,
        playwright_factory: Callable[
            ..., AbstractAsyncContextManager[Any]
        ] = AsyncCamoufox,
        solver_factory: Callable[
            ..., AbstractAsyncContextManager[Any]
        ] = ManagedClickSolver,
    ) -> None:
        """Configure the Camoufox and challenge-solver constructors."""
        self._playwright_factory = playwright_factory
        self._solver_factory = solver_factory

    async def open(self, proxy: ProxySettings) -> BrowserResource:
        """Enter a browser scope and create a context and page within it."""
        scope = self._playwright_factory(
            main_world_eval=True,
            addons=[ADDON_PATH],
            geoip=True,
            headless=True,
            proxy=proxy.as_playwright_proxy(),
            humanize=True,
            locale=BROWSER_LOCALE or "en-US",
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
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
        solver_scope = self._solver_factory(
            framework=FrameworkType.CAMOUFOX,
            page=page,
            max_attempts=MAX_ATTEMPTS,
            attempt_delay=1,
        )
        try:
            solver = cast("ClickSolver", await solver_scope.__aenter__())
        except BaseException as error:
            await self._close_after_open_failure(scope, context, error)
            raise
        return BrowserResource(
            page=page,
            context=context,
            solver=solver,
            _scope=scope,
            _solver_scope=solver_scope,
        )

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
