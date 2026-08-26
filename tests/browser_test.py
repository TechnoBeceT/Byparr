# ruff: noqa: D102, D103, D105, D107, S106

import asyncio
from collections.abc import AsyncGenerator

import pytest

from src import utils
from src.browser import BrowserFactory
from src.proxy import ProxySettings


class FakePage:
    pass


class FakeContext:
    def __init__(self, *, page_error: BaseException | None = None) -> None:
        self.page_error = page_error
        self.page = FakePage()
        self.close_calls = 0

    async def new_page(self) -> FakePage:
        if self.page_error is not None:
            raise self.page_error
        return self.page

    async def close(self) -> None:
        self.close_calls += 1


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
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.exit_calls = 0

    async def __aenter__(self) -> FakeBrowser:
        return self.browser

    async def __aexit__(self, *args: object) -> None:
        self.exit_calls += 1


class FakeBrowserFactory:
    def __init__(self, resource) -> None:
        self.resource = resource
        self.proxies: list[ProxySettings] = []

    async def open(self, proxy: ProxySettings):
        self.proxies.append(proxy)
        return self.resource


class FakeResource:
    def __init__(self) -> None:
        self.page = FakePage()
        self.context = FakeContext()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def make_factory(scope: FakePlaywrightScope):
    return BrowserFactory(playwright_factory=lambda **_: scope)


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
    assert dependency_value == utils.BrowserDepClass(resource.page, resource.context)

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
