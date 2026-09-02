# ruff: noqa: D102, D103, D105, D107, S106, SLF001

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.async_api import BrowserContext, Page
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType

from src import utils
from src.browser import (
    BrowserFactory,
    FatalSolverBrowserError,
    ManagedBrowserResource,
    ManagedClickSolver,
)
from src.consts import MAX_ATTEMPTS
from src.proxy import ProxySettings


class FakePage:
    pass


class FakeContext:
    def __init__(
        self,
        *,
        page_error: BaseException | None = None,
        close_error: BaseException | None = None,
        close_started: asyncio.Event | None = None,
        allow_close: asyncio.Event | None = None,
    ) -> None:
        self.page_error = page_error
        self.close_error = close_error
        self.close_started = close_started
        self.allow_close = allow_close
        self.page = FakePage()
        self.close_calls = 0

    async def new_page(self) -> FakePage:
        if self.page_error is not None:
            raise self.page_error
        return self.page

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.allow_close is not None:
            await self.allow_close.wait()
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(
        self,
        *,
        context: FakeContext | None = None,
        context_error: BaseException | None = None,
    ) -> None:
        self.context = context
        self.context_error = context_error

    async def new_context(self) -> FakeContext:
        if self.context_error is not None:
            raise self.context_error
        assert self.context is not None
        return self.context


class FakePlaywrightScope:
    def __init__(
        self,
        browser: FakeBrowser | None = None,
        *,
        enter_error: BaseException | None = None,
        exit_error: BaseException | None = None,
    ) -> None:
        self.browser = browser
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.exit_calls = 0

    async def __aenter__(self) -> FakeBrowser:
        if self.enter_error is not None:
            raise self.enter_error
        assert self.browser is not None
        return self.browser

    async def __aexit__(self, *args: object) -> None:
        self.exit_calls += 1
        if self.exit_error is not None:
            raise self.exit_error


class FakeSolver:
    async def solve_captcha(self, **_kwargs: object) -> None:
        return None


class FakeSolverScope:
    def __init__(self) -> None:
        self.solver = FakeSolver()
        self.exit_calls = 0

    async def __aenter__(self) -> FakeSolver:
        return self.solver

    async def __aexit__(self, *args: object) -> None:
        self.exit_calls += 1


class FakeBrowserFactory:
    def __init__(self, resource: ManagedBrowserResource) -> None:
        self.resource = resource
        self.proxies: list[ProxySettings] = []

    async def open(self, proxy: ProxySettings) -> ManagedBrowserResource:
        self.proxies.append(proxy)
        return self.resource


class FakeResource:
    def __init__(self) -> None:
        self.page = FakePage()
        self.context = FakeContext()
        self.solver = FakeSolver()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def make_factory(
    scope: FakePlaywrightScope,
    solver_scope: FakeSolverScope | None = None,
    browser_options: dict[str, object] | None = None,
    solver_options: dict[str, object] | None = None,
) -> BrowserFactory:
    def build_scope(**options: object) -> FakePlaywrightScope:
        if browser_options is not None:
            browser_options.update(options)
        return scope

    selected_solver_scope = solver_scope or FakeSolverScope()

    def build_solver(**options: object) -> FakeSolverScope:
        if solver_options is not None:
            solver_options.update(options)
        return selected_solver_scope

    factory = cast(
        "Callable[..., AbstractAsyncContextManager[Any]]",
        build_scope,
    )
    solver_factory = cast(
        "Callable[..., AbstractAsyncContextManager[Any]]",
        build_solver,
    )
    return BrowserFactory(
        playwright_factory=factory,
        solver_factory=solver_factory,
    )


@pytest.mark.asyncio
async def test_browser_factory_owns_camoufox_solver_for_resource_lifetime() -> None:
    context = FakeContext()
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    solver_scope = FakeSolverScope()
    browser_options: dict[str, object] = {}
    solver_options: dict[str, object] = {}

    resource = await make_factory(
        scope, solver_scope, browser_options, solver_options
    ).open(ProxySettings.direct())

    assert resource.solver is solver_scope.solver
    assert browser_options["main_world_eval"] is True
    assert browser_options["geoip"] is True
    assert browser_options["disable_coop"] is True
    assert solver_options["page"] is context.page
    assert solver_options["attempt_delay"] == 1
    assert solver_options["max_attempts"] == MAX_ATTEMPTS
    await resource.close()

    assert solver_scope.exit_calls == 1
    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_managed_click_solver_escapes_dependency_retry_on_target_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fatal closure crosses the real dependency loop without a second attempt."""
    solve_once = AsyncMock(side_effect=TargetClosedError("closed"))
    monkeypatch.setattr(ClickSolver, "_solve_captcha_once", solve_once)
    solver = ManagedClickSolver(
        framework=FrameworkType.CAMOUFOX,
        page=MagicMock(),
        max_attempts=MAX_ATTEMPTS,
        attempt_delay=0,
    )
    solver._prepare_called = True
    solver._get_solver_data = AsyncMock(return_value={})

    with pytest.raises(FatalSolverBrowserError) as exc:
        await solver.solve_captcha(
            captcha_container=MagicMock(),
            captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
        )

    assert not isinstance(exc.value, Exception)
    assert isinstance(exc.value.__cause__, TargetClosedError)
    solve_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_click_solver_preserves_dependency_retry_for_nonfatal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only fatal closure bypasses ClickSolver's established retry policy."""
    solve_once = AsyncMock(side_effect=[RuntimeError("retry"), None])
    expected_attempts = 2
    monkeypatch.setattr(ClickSolver, "_solve_captcha_once", solve_once)
    solver = ManagedClickSolver(
        framework=FrameworkType.CAMOUFOX,
        page=MagicMock(),
        max_attempts=expected_attempts,
        attempt_delay=0,
    )
    solver._prepare_called = True
    solver._get_solver_data = AsyncMock(return_value={})

    await solver.solve_captcha(
        captcha_container=MagicMock(),
        captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
    )

    assert solve_once.await_count == expected_attempts


@pytest.mark.asyncio
async def test_browser_resource_closes_its_context_and_scope_once() -> None:
    context = FakeContext()
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())

    assert resource.page is context.page
    assert resource.context is context

    await resource.close()
    await resource.close()

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_browser_factory_exits_scope_when_context_creation_fails() -> None:
    scope = FakePlaywrightScope(FakeBrowser(context_error=RuntimeError("context")))

    with pytest.raises(RuntimeError, match="context"):
        await make_factory(scope).open(ProxySettings.direct())

    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_browser_factory_exits_scope_when_entry_fails_and_preserves_entry_error() -> (
    None
):
    scope = FakePlaywrightScope(
        enter_error=RuntimeError("entry"), exit_error=RuntimeError("cleanup")
    )

    with pytest.raises(RuntimeError, match="entry"):
        await make_factory(scope).open(ProxySettings.direct())

    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_browser_factory_closes_context_and_exits_scope_when_page_creation_fails() -> (
    None
):
    context = FakeContext(page_error=RuntimeError("page"))
    scope = FakePlaywrightScope(FakeBrowser(context=context))

    with pytest.raises(RuntimeError, match="page"):
        await make_factory(scope).open(ProxySettings.direct())

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_concurrent_browser_resource_closers_wait_for_one_shared_cleanup() -> (
    None
):
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    context = FakeContext(close_started=close_started, allow_close=allow_close)
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())

    first_closer = asyncio.create_task(resource.close())
    await close_started.wait()
    second_closer = asyncio.create_task(resource.close())
    await asyncio.sleep(0)

    assert not first_closer.done()
    assert not second_closer.done()

    allow_close.set()
    await asyncio.gather(first_closer, second_closer)

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_cancelled_browser_resource_closer_does_not_cancel_shared_cleanup() -> (
    None
):
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    context = FakeContext(close_started=close_started, allow_close=allow_close)
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())

    cancelled_closer = asyncio.create_task(resource.close())
    await close_started.wait()
    cancelled_closer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_closer

    assert context.close_calls == 1
    assert scope.exit_calls == 0

    allow_close.set()
    await resource.close()

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_cancelled_closer_leaves_no_unretrieved_cleanup_error() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    context = FakeContext(
        close_error=RuntimeError("context cleanup"),
        close_started=close_started,
        allow_close=allow_close,
    )
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())
    loop = asyncio.get_running_loop()
    reports: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, report: reports.append(report))
    try:
        cancelled_closer = asyncio.create_task(resource.close())
        await close_started.wait()
        cancelled_closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_closer

        allow_close.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del resource
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert reports == []


@pytest.mark.asyncio
async def test_browser_resource_replays_shared_cleanup_error_to_later_closers() -> None:
    context = FakeContext(close_error=RuntimeError("context cleanup"))
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())

    with pytest.raises(RuntimeError, match="context cleanup"):
        await resource.close()
    with pytest.raises(RuntimeError, match="context cleanup"):
        await resource.close()

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
async def test_dependency_cancellation_does_not_cancel_resource_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    context = FakeContext(close_started=close_started, allow_close=allow_close)
    scope = FakePlaywrightScope(FakeBrowser(context=context))
    resource = await make_factory(scope).open(ProxySettings.direct())
    monkeypatch.setattr(utils, "BrowserFactory", lambda: FakeBrowserFactory(resource))
    dependency = utils.get_browser()
    await anext(dependency)

    closing_dependency = asyncio.create_task(dependency.aclose())
    await close_started.wait()
    closing_dependency.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing_dependency

    assert scope.exit_calls == 0

    allow_close.set()
    await resource.close()

    assert context.close_calls == 1
    assert scope.exit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["normal", "error", "cancelled"])
async def test_disposable_browser_dependency_closes_resource_on_every_exit(
    monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    resource = FakeResource()
    factory = FakeBrowserFactory(resource)
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr(utils, "PROXY_SERVER", "http://environment-proxy.test:8080")
    monkeypatch.setattr(utils, "PROXY_USERNAME", "environment-user")
    monkeypatch.setattr(utils, "PROXY_PASSWORD", "environment-password")

    dependency: AsyncGenerator[utils.BrowserDepClass] = utils.get_browser(
        x_proxy_server="http://header-proxy.test:8080",
        x_proxy_username="header-user",
        x_proxy_password="header-password",
    )
    dependency_value = await anext(dependency)
    assert dependency_value == utils.BrowserDepClass(
        cast("Page", resource.page),
        cast("BrowserContext", resource.context),
        cast("Any", resource.solver),
    )

    if finish == "normal":
        await dependency.aclose()
    elif finish == "error":
        with pytest.raises(RuntimeError, match="request failure"):
            await dependency.athrow(RuntimeError("request failure"))
    else:
        with pytest.raises(asyncio.CancelledError):
            await dependency.athrow(asyncio.CancelledError())

    assert factory.proxies == [
        ProxySettings(
            server="http://header-proxy.test:8080",
            username="header-user",
            password="header-password",
        )
    ]
    assert resource.close_calls == 1
