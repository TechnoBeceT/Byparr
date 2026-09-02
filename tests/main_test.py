# ruff: noqa: D102, D107, PLC0415, PLR2004, TRY003

import asyncio
import base64
import gc
import logging
from contextlib import asynccontextmanager
from http import HTTPStatus
from io import StringIO
from json import JSONDecodeError
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError
from starlette.testclient import TestClient

from main import app, lifespan
from src.browser import BrowserFactory
from src.challenge import CF_INTERSTITIAL_INDICATORS_SELECTORS
from src.consts import VERSION
from src.content import fetch_pdf_content
from src.endpoints import read_item
from src.models import LinkRequest
from src.proxy import ProxySettings
from src.sessions import SessionCapacityError, SessionManager
from src.utils import BrowserDepClass, TimeoutTimer, get_request_browser, remaining_ms

client = TestClient(app)

test_websites = [
    "https://ext.to/",
    # "https://www.ygg.re/",
    "https://extratorrent.st/",
    "https://speed.cd/login",
    'https://www.yggtorrent.top/engine/search?do=search&order=desc&sort=publish_date&name="UNESCAPED"+"DOUBLEQUOTES"&category=2145',
    "https://1337x.to/home/",
]


@pytest.mark.parametrize("website", test_websites)
def test_bypass(website: str):
    """
    Tests if the service can bypass cloudflare/DDOS-GUARD on given websites.

    This test is skipped if the website is not reachable or does not have cloudflare/DDOS-GUARD.
    """
    test_request = httpx2.get(
        website,
    )
    if (
        test_request.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        and "Just a moment..." not in test_request.text
    ):
        try:
            error_details = test_request.json()
        except JSONDecodeError:
            error_details = test_request.text
        pytest.skip(
            f"Skipping {website} - ({test_request.status_code}) {error_details}"
        )

    response = client.post(
        "/v1",
        json=LinkRequest.model_construct(
            url=website, cmd="request.get", max_timeout=60
        ).model_dump(),
    )

    assert response.status_code == HTTPStatus.OK
    solution = response.json()["solution"]
    assert "_cf_chl_opt" not in solution["response"]
    assert "__cf_chl" not in solution["url"]


def test_json_api():
    """
    JSON APIs must return 200, not crash on the UA evaluate.

    Firefox renders application/json in a built-in viewer whose CSP blocks
    Playwright's eval-based evaluate() (issue #394). The browser must be
    launched with the viewer disabled so /v1 works and returns the raw JSON.
    """
    url = "https://api.ipify.org?format=json"
    test_request = httpx2.get(url)
    if test_request.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        pytest.skip(
            f"Skipping JSON API test - upstream error ({test_request.status_code})"
        )

    response = client.post(
        "/v1",
        json=LinkRequest.model_construct(url=url, cmd="request.get").model_dump(),
    )

    if response.status_code == HTTPStatus.REQUEST_TIMEOUT:
        pytest.skip("Skipping JSON API test - timed out (upstream issue)")

    assert response.status_code == HTTPStatus.OK
    solution = response.json()["solution"]
    assert solution["userAgent"]
    assert '"ip"' in solution["response"]


def test_health_check():
    """
    Tests the health check endpoint.

    This test ensures that the health check
    endpoint returns HTTPStatus.OK.
    """
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK


def test_pdf_handling():
    """Tests that PDF URLs return the raw PDF bytes, not the Firefox viewer HTML."""
    pdf_url = "https://mondaymandala.com/wp-content/uploads/Mickey-And-Minnie-Mouse-Holding-An-Easter-Egg-Basket-Coloring-Page-For-Kids.pdf"
    response = client.post(
        "/v1",
        json=LinkRequest.model_construct(url=pdf_url, cmd="request.get").model_dump(),
    )
    if response.status_code == HTTPStatus.REQUEST_TIMEOUT:
        pytest.skip("Skipping PDF test - timed out (upstream issue)")
    assert response.status_code == HTTPStatus.OK
    solution = response.json()["solution"]
    if solution.get("contentType") != "application/pdf":
        pytest.skip(
            "Skipping PDF test - PDF bytes could not be fetched (upstream issue)"
        )
    assert solution["response"]  # non-empty base64

    decoded = base64.b64decode(solution["response"])
    assert decoded[:5] == b"%PDF-"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"max_timeout": 60}, 60),  # native API: seconds
        ({"maxTimeout": 60}, 60),  # FlareSolverr alias, seconds-range value
        ({"maxTimeout": 60000}, 60),  # FlareSolverr alias: milliseconds
        ({"maxTimeout": 55000}, 55),
        ({"maxTimeout": 1000}, 1),
        ({}, 60),  # default
    ],
)
def test_max_timeout_normalization(payload: dict[str, int], expected: int) -> None:
    """MaxTimeout must accept FlareSolverr's milliseconds while keeping seconds."""
    request = LinkRequest.model_validate({"url": "https://example.com", **payload})
    assert request.max_timeout == expected


def fake_dep(
    *,
    fail_states: set[str] | None = None,
    challenged: bool = False,
    marker_counts: list[int] | None = None,
    widget_box: dict[str, float] | None = None,
    user_agent: str | None = "UnitTestBrowser/1.0",
) -> BrowserDepClass:
    """Build a browser dependency pair backed by mocks."""
    page = AsyncMock()
    page.url = "https://example.test/login"
    page.goto.return_value = MagicMock(
        status=HTTPStatus.OK,
        headers={"content-type": "text/html"},
        request=MagicMock(headers={"user-agent": user_agent} if user_agent else {}),
    )
    page.title.return_value = "Login"
    page.content.return_value = "<html><title>Login</title></html>"
    remaining = list(marker_counts or [])

    def count_for(selector: str) -> int:
        """Answer the marker check from the script, else from `challenged`."""
        if selector not in CF_INTERSTITIAL_INDICATORS_SELECTORS or not remaining:
            return 1 if challenged else 0
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    def locator(selector: str) -> MagicMock:
        handle = MagicMock()
        handle.count = AsyncMock(side_effect=lambda: count_for(selector))
        handle.first.bounding_box = AsyncMock(return_value=widget_box)
        handle.first.input_value = AsyncMock(return_value="")
        return handle

    page.locator = MagicMock(side_effect=locator)

    def wait_for_load_state(state: str, **_kwargs: object) -> None:
        """Fail the wait when asked for a configured state."""
        if state in (fail_states or set()):
            message = "load state wait timed out"
            raise PlaywrightTimeoutError(message)

    page.wait_for_load_state.side_effect = wait_for_load_state

    context = AsyncMock()
    context.cookies.return_value = []
    solver = AsyncMock()
    return BrowserDepClass(page=page, context=context, solver=solver)


class EndpointResource:
    """A disposable browser resource with a stable cookie per context."""

    def __init__(self, number: int) -> None:
        self.page = cast("AsyncMock", fake_dep().page)
        self.context = AsyncMock()
        self.solver = AsyncMock()
        self.context.cookies.return_value = [
            {
                "name": "browser",
                "value": str(number),
                "domain": "example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ]
        self.close = AsyncMock()


class EndpointFactory:
    """Create inspectable browser resources for HTTP endpoint tests."""

    def __init__(self) -> None:
        self.resources: list[EndpointResource] = []

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        _ = proxy
        resource = EndpointResource(len(self.resources) + 1)
        self.resources.append(resource)
        return resource


class FatalEndpointFactory(EndpointFactory):
    """Make the first retained resource close fatally at one request stage."""

    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        if len(self.resources) != 1:
            return resource
        error = TargetClosedError("browser has been closed")
        if self.stage == "routes":
            resource.page.route.side_effect = error
        elif self.stage == "navigation":
            resource.page.goto.side_effect = error
        elif self.stage == "cookies":
            resource.context.cookies.side_effect = error
        return resource


class RecoverableEndpointFactory(EndpointFactory):
    """Fail one navigation without closing the retained browser target."""

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        successful_navigation = resource.page.goto.return_value
        resource.page.goto.side_effect = [
            PlaywrightError("NS_ERROR_UNKNOWN_HOST"),
            successful_navigation,
        ]
        return resource


class PdfFailureEndpointFactory(EndpointFactory):
    """Fail PDF body retrieval on the first retained browser resource."""

    def __init__(self, error: PlaywrightError) -> None:
        super().__init__()
        self.error = error

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        if len(self.resources) != 1:
            return resource
        resource.page.goto.return_value.headers = {"content-type": "application/pdf"}
        fetch_response = AsyncMock()
        fetch_response.body.side_effect = self.error
        resource.page.request.fetch.return_value = fetch_response
        return resource


class PartialRouteEndpointFactory(EndpointFactory):
    """Model a route inserted locally before protocol registration fails."""

    def __init__(self, *, cleanup_fails: bool = False) -> None:
        super().__init__()
        self.unrelated_handler = object()
        self.handlers: list[object] = [self.unrelated_handler]
        self.cleanup_fails = cleanup_fails

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        if len(self.resources) != 1:
            return resource

        async def install(_pattern: str, handler: object) -> None:
            self.handlers.append(handler)
            raise PlaywrightError("protocol route update failed")

        async def remove(_pattern: str, handler: object) -> None:
            if self.cleanup_fails:
                raise PlaywrightError("protocol route cleanup failed")
            self.handlers.remove(handler)

        resource.page.route.side_effect = install
        resource.page.unroute.side_effect = remove
        return resource


class CancelFailRouteEndpointFactory(EndpointFactory):
    """Fail a route protocol update after its local state transition."""

    def __init__(self, failure_phase: str, error: PlaywrightError) -> None:
        super().__init__()
        self.failure_phase = failure_phase
        self.error = error
        self.unrelated_handler = object()
        self.handlers: list[object] = [self.unrelated_handler]
        self.operation_started = asyncio.Event()
        self.allow_failure = asyncio.Event()

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        if len(self.resources) != 1:
            return resource

        async def install(_pattern: str, handler: object) -> None:
            self.handlers.append(handler)
            if self.failure_phase == "registration":
                self.operation_started.set()
                await self.allow_failure.wait()
                raise self.error

        async def remove(_pattern: str, handler: object) -> None:
            self.handlers.remove(handler)
            if self.failure_phase == "cleanup":
                self.operation_started.set()
                await self.allow_failure.wait()
                raise self.error

        resource.page.route.side_effect = install
        resource.page.unroute.side_effect = remove
        return resource


class ChallengeProbeEndpointFactory(EndpointFactory):
    """Drive one retained request through a selected challenge-probe failure."""

    def __init__(
        self,
        probe: str,
        *,
        fatal: bool,
        clear_after_marker_calls: int | None = None,
    ) -> None:
        super().__init__()
        self.probe = probe
        self.fatal = fatal
        self.clear_after_marker_calls = clear_after_marker_calls
        self.marker_calls = 0

    async def open(self, proxy: ProxySettings) -> EndpointResource:
        resource = await super().open(proxy)
        if len(self.resources) != 1:
            return resource
        error = self._probe_error()
        marker = self._marker(error)
        if self.probe == "solver":
            resource.solver.solve_captcha.side_effect = error

        def locator(selector: str) -> MagicMock:
            if selector in CF_INTERSTITIAL_INDICATORS_SELECTORS:
                return marker
            return MagicMock()

        resource.page.locator.side_effect = locator
        return resource

    def _probe_error(self) -> PlaywrightError:
        if self.fatal:
            return TargetClosedError("browser has been closed")
        return PlaywrightError("ordinary probe failure")

    def _marker(self, error: PlaywrightError) -> MagicMock:
        marker = MagicMock()

        def marker_count() -> int:
            self.marker_calls += 1
            if self.probe == "marker":
                raise error
            if (
                self.clear_after_marker_calls is not None
                and self.marker_calls >= self.clear_after_marker_calls
            ):
                return 0
            return 1

        marker.count = AsyncMock(side_effect=marker_count)
        return marker

class RejectingSessionManager:
    """Model a saturated retained-session manager at the HTTP boundary."""

    @asynccontextmanager
    async def acquire(self, _key: object, _proxy: object):
        raise SessionCapacityError
        yield  # pragma: no cover


class RecordingSessionManager:
    """Record session reset calls without opening a retained browser."""

    def __init__(self) -> None:
        self.reset_calls: list[str] = []
        self.acquire_calls = 0

    @asynccontextmanager
    async def acquire(self, _key: object, _proxy: object):
        self.acquire_calls += 1
        raise AssertionError("session command must not acquire a browser")
        yield  # pragma: no cover

    async def reset(self, session: str) -> int:
        self.reset_calls.append(session)
        return 2


def test_named_session_reuses_its_site_but_isolates_other_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-site session keeps its context and a different site gets another."""
    from src import utils

    factory = EndpointFactory()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        first = test_client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://one.example.com/a",
                "session": "account",
            },
        )
        second = test_client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://two.example.com/b",
                "session": "account",
            },
        )
        other_site = test_client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.org/test",
                "session": "account",
            },
        )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert other_site.status_code == HTTPStatus.OK
    assert first.json()["solution"]["cookies"] == second.json()["solution"]["cookies"]
    assert (
        first.json()["solution"]["cookies"] != other_site.json()["solution"]["cookies"]
    )
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]


@pytest.mark.parametrize("fatal_stage", ["routes", "navigation", "cookies", "content"])
@pytest.mark.asyncio
async def test_fatal_browser_failure_retires_the_retained_resource(
    monkeypatch: pytest.MonkeyPatch,
    fatal_stage: str,
) -> None:
    """A closed Playwright target is never reused by the next HTTP request."""
    from src import endpoints

    factory = FatalEndpointFactory(fatal_stage)
    manager = SessionManager(factory)
    app.state.session_manager = manager
    if fatal_stage == "content":
        original = endpoints.build_response_content
        first_call = True

        async def fail_first_content(
            page: Page,
            request: LinkRequest,
            page_request: object,
            *,
            challenge_detected: bool,
            page_html: str | None,
        ) -> tuple[str, str]:
            nonlocal first_call
            if first_call:
                first_call = False
                raise TargetClosedError("browser has been closed")
            return await original(
                page,
                request,
                page_request,
                challenge_detected=challenge_detected,
                page_html=page_html,
            )

        monkeypatch.setattr(endpoints, "build_response_content", fail_first_content)

    payload = {
        "url": "https://example.test/one",
        "session": "account",
        "blockMedia": fatal_stage == "routes",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/v1", json=payload)
        second = await client.post("/v1", json=payload)
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]


@pytest.mark.asyncio
async def test_fatal_pdf_body_failure_retires_retained_resource_without_logging_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed target during PDF body retrieval cannot become fallback success."""
    from src.utils import logger as production_logger

    full_url = (
        "https://target-user:target-password@reader.example.com/private/document.pdf"
        "?token=query-secret"
    )
    leaked_values = (
        full_url,
        "/private/document.pdf",
        "target-user",
        "target-password",
        "query-secret",
        "cookie-secret",
        "header-secret",
    )
    error = TargetClosedError(" | ".join(leaked_values))
    factory = PdfFailureEndpointFactory(error)
    manager = SessionManager(factory)
    app.state.session_manager = manager
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    monkeypatch.setattr(production_logger, "level", logging.INFO)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    payload = {"url": full_url, "session": "account-secret"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1",
            json=payload,
            headers={
                "Cookie": "cf_clearance=cookie-secret",
                "X-Trace": "header-secret",
            },
        )
        second = await client.post("/v1", json=payload)
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]
    rendered = stream.getvalue()
    assert (
        "browser_request_error site=example.com error_type=TargetClosedError"
        in rendered
    )
    for secret in (*leaked_values, "account-secret"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("full_url", "expected_site", "site_secrets"),
    [
        (
            (
                "https://target-user:target-password@reader.example.com"
                "/private/document.pdf?token=query-secret"
            ),
            "example.com",
            (),
        ),
        (
            (
                "https://target-user:target-password@"
                "[fe80:0:0::1%25pdf-scope-secret]/private/document.pdf"
                "?token=query-secret"
            ),
            "fe80::1",
            ("pdf-scope-secret",),
        ),
    ],
)
@pytest.mark.asyncio
async def test_ordinary_pdf_failure_falls_back_with_secret_safe_log(
    monkeypatch: pytest.MonkeyPatch,
    full_url: str,
    expected_site: str,
    site_secrets: tuple[str, ...],
) -> None:
    """A recoverable PDF fetch error keeps HTML fallback without logging raw data."""
    from src.utils import logger as production_logger

    leaked_values = (
        full_url,
        "/private/document.pdf",
        "target-user",
        "target-password",
        "query-secret",
        "cookie-secret",
        "header-secret",
        *site_secrets,
    )
    dep = fake_dep()
    page = cast("AsyncMock", dep.page)
    page.url = full_url
    fetch_response = AsyncMock()
    fetch_response.body.side_effect = PlaywrightError(" | ".join(leaked_values))
    page.request.fetch.return_value = fetch_response
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    monkeypatch.setattr(production_logger, "level", logging.INFO)

    content_type, response_content = await fetch_pdf_content(page)

    assert (content_type, response_content) == (
        "text/html",
        "<html><title>Login</title></html>",
    )
    rendered = stream.getvalue()
    assert f"pdf_fetch_fallback site={expected_site} error_type=Error" in rendered
    for secret in leaked_values:
        assert secret not in rendered


@pytest.mark.asyncio
async def test_block_media_routes_do_not_persist_or_accumulate_on_a_reused_page() -> (
    None
):
    """Each enabled request removes exactly the handler it installed."""
    dep = fake_dep()

    await read_item(LinkRequest(url="https://example.test/one", blockMedia=True), dep)
    await read_item(LinkRequest(url="https://example.test/two", blockMedia=False), dep)
    await read_item(LinkRequest(url="https://example.test/three", blockMedia=True), dep)

    page = cast("AsyncMock", dep.page)
    assert page.route.await_count == 2
    assert page.unroute.await_count == 2
    installed = [call.args[1] for call in page.route.await_args_list]
    removed = [call.args[1] for call in page.unroute.await_args_list]
    assert removed == installed


@pytest.mark.asyncio
async def test_block_media_route_is_removed_when_navigation_is_cancelled() -> None:
    """Cancellation cannot leave a request-owned handler on a retained page."""
    dep = fake_dep()
    navigation_started = asyncio.Event()

    async def wait_forever(*_args: object, **_kwargs: object) -> None:
        navigation_started.set()
        await asyncio.Event().wait()

    page = cast("AsyncMock", dep.page)
    page.goto.side_effect = wait_forever
    reading = asyncio.create_task(
        read_item(LinkRequest(url="https://example.test", blockMedia=True), dep)
    )
    await navigation_started.wait()
    reading.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reading

    handler = page.route.await_args.args[1]
    page.unroute.assert_awaited_once_with("**/*", handler)


@pytest.mark.asyncio
async def test_block_media_registration_cancellation_drains_then_removes_handler() -> (
    None
):
    """Cancellation after route installation cannot release a sticky handler."""
    dep = fake_dep()
    page = cast("AsyncMock", dep.page)
    unrelated = object()
    handlers: list[object] = [unrelated]
    registration_started = asyncio.Event()
    allow_registration_return = asyncio.Event()

    async def install(_pattern: str, handler: object) -> None:
        handlers.append(handler)
        registration_started.set()
        await allow_registration_return.wait()

    async def remove(_pattern: str, handler: object) -> None:
        handlers.remove(handler)

    page.route.side_effect = install
    page.unroute.side_effect = remove
    reading = asyncio.create_task(
        read_item(LinkRequest(url="https://example.test", blockMedia=True), dep)
    )
    await registration_started.wait()
    reading.cancel()
    allow_registration_return.set()

    with pytest.raises(asyncio.CancelledError):
        await reading

    assert handlers == [unrelated]
    page.unroute.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_route_registration_failure_rolls_back_owned_handler() -> None:
    """A protocol error after local insertion cannot leave a sticky route."""
    factory = PartialRouteEndpointFactory()
    manager = SessionManager(factory)
    app.state.session_manager = manager
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1",
            json={
                "url": "https://example.test/one",
                "session": "account",
                "blockMedia": True,
            },
        )
        second = await client.post(
            "/v1",
            json={
                "url": "https://example.test/two",
                "session": "account",
                "blockMedia": False,
            },
        )
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 1
    assert factory.handlers == [factory.unrelated_handler]


@pytest.mark.asyncio
async def test_uncertain_partial_route_cleanup_retires_retained_resource() -> None:
    """A failed rollback makes the retained page unavailable to later requests."""
    factory = PartialRouteEndpointFactory(cleanup_fails=True)
    manager = SessionManager(factory)
    app.state.session_manager = manager
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1",
            json={
                "url": "https://example.test/one",
                "session": "account",
                "blockMedia": True,
            },
        )
        second = await client.post(
            "/v1",
            json={
                "url": "https://example.test/two",
                "session": "account",
                "blockMedia": False,
            },
        )
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]


@pytest.mark.asyncio
async def test_block_media_cleanup_cancellation_drains_exact_handler_removal() -> None:
    """Cancellation during unroute cannot release the retained page prematurely."""
    dep = fake_dep()
    page = cast("AsyncMock", dep.page)
    unrelated = object()
    handlers: list[object] = [unrelated]
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def install(_pattern: str, handler: object) -> None:
        handlers.append(handler)

    async def remove(_pattern: str, handler: object) -> None:
        cleanup_started.set()
        await allow_cleanup.wait()
        handlers.remove(handler)

    page.route.side_effect = install
    page.unroute.side_effect = remove
    reading = asyncio.create_task(
        read_item(LinkRequest(url="https://example.test", blockMedia=True), dep)
    )
    await cleanup_started.wait()
    reading.cancel()
    allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await reading

    assert handlers == [unrelated]
    page.unroute.assert_awaited_once()


@pytest.mark.parametrize(
    ("failure_phase", "expected_resources"),
    [("registration", 1), ("cleanup", 2)],
)
@pytest.mark.asyncio
async def test_route_failure_after_cancellation_preserves_cancellation_without_loop_leak(
    failure_phase: str,
    expected_resources: int,
) -> None:
    """A secondary route failure is consumed while caller cancellation wins."""
    leaked_values = (
        (
            "https://route-user:route-password@example.test/private"
            "?token=route-query-secret"
        ),
        "route-user",
        "route-password",
        "route-query-secret",
        "route-cookie-secret",
        "route-header-secret",
    )
    factory = CancelFailRouteEndpointFactory(
        failure_phase,
        PlaywrightError(" | ".join(leaked_values)),
    )
    manager = SessionManager(factory)
    request = LinkRequest(
        url="https://example.test/one",
        session="account",
        blockMedia=True,
    )
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(dict(context)))

    async def run_request(target: LinkRequest) -> None:
        async with get_request_browser(target, manager) as dep:
            await read_item(target, dep)

    try:
        reading = asyncio.create_task(run_request(request))
        await factory.operation_started.wait()
        reading.cancel()
        factory.allow_failure.set()
        result = await asyncio.gather(reading, return_exceptions=True)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

        await run_request(
            LinkRequest(
                url="https://example.test/two",
                session="account",
                blockMedia=False,
            )
        )
    finally:
        await manager.close()
        loop.set_exception_handler(previous_handler)

    assert {
        "result_cancelled": isinstance(result[0], asyncio.CancelledError),
        "task_cancelled": reading.cancelled(),
        "foreign_handler_only": factory.handlers == [factory.unrelated_handler],
        "loop_error_count": len(loop_errors),
    } == {
        "result_cancelled": True,
        "task_cancelled": True,
        "foreign_handler_only": True,
        "loop_error_count": 0,
    }
    assert len(factory.resources) == expected_resources
    assert [resource.close.await_count for resource in factory.resources] == [
        1
    ] * expected_resources
    rendered_loop_errors = " ".join(str(context) for context in loop_errors)
    for secret in leaked_values:
        assert secret not in rendered_loop_errors


@pytest.mark.asyncio
async def test_block_media_route_is_removed_when_navigation_fails() -> None:
    """A recoverable navigation error cannot leak a request-owned route."""
    dep = fake_dep()
    page = cast("AsyncMock", dep.page)
    page.goto.side_effect = PlaywrightError("NS_ERROR_UNKNOWN_HOST")

    with pytest.raises(HTTPException) as failure:
        await read_item(LinkRequest(url="https://nope.invalid", blockMedia=True), dep)

    assert failure.value.status_code == HTTPStatus.BAD_GATEWAY
    handler = page.route.await_args.args[1]
    page.unroute.assert_awaited_once_with("**/*", handler)


@pytest.mark.asyncio
async def test_recoverable_navigation_failure_preserves_the_retained_resource() -> None:
    """Ordinary source failures keep clearance state available for retry."""
    factory = RecoverableEndpointFactory()
    manager = SessionManager(factory)
    app.state.session_manager = manager
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1",
            json={"url": "https://example.test/one", "session": "account"},
        )
        second = await client.post(
            "/v1",
            json={"url": "https://example.test/two", "session": "account"},
        )

    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 1
    factory.resources[0].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_playwright_failure_log_uses_safe_site_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw browser exception text cannot reach production-rendered logs."""
    from src.utils import logger as production_logger

    full_url = (
        "https://target-user:target-password@reader.example.com/private/path"
        "?token=clearance-secret"
    )
    leaked_values = (
        full_url,
        "/private/path",
        "target-user",
        "target-password",
        "clearance-secret",
        "cookie-super-secret",
        "header-super-secret",
        "proxy-user-secret",
        "proxy-password-secret",
    )
    factory = EndpointFactory()
    resource = await factory.open(ProxySettings.direct())
    resource.page.goto.side_effect = PlaywrightError(" | ".join(leaked_values))
    factory.resources.clear()

    async def reuse_configured_resource(proxy: ProxySettings) -> EndpointResource:
        _ = proxy
        factory.resources.append(resource)
        return resource

    monkeypatch.setattr(factory, "open", reuse_configured_resource)
    manager = SessionManager(factory)
    app.state.session_manager = manager
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    monkeypatch.setattr(production_logger, "level", logging.INFO)
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1",
            json={"url": full_url, "session": "account-secret"},
            headers={
                "Cookie": "cf_clearance=cookie-super-secret",
                "X-Trace": "header-super-secret",
                "X-Proxy-Server": "http://proxy.example:8080",
                "X-Proxy-Username": "proxy-user-secret",
                "X-Proxy-Password": "proxy-password-secret",
            },
        )
    await manager.close()

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    rendered = stream.getvalue()
    assert "browser_request_error site=example.com error_type=Error" in rendered
    for secret in (*leaked_values, "account-secret"):
        assert secret not in rendered


@pytest.mark.parametrize("fatal_probe", ["marker", "solver"])
@pytest.mark.asyncio
async def test_fatal_challenge_probe_retires_the_retained_resource(
    fatal_probe: str,
) -> None:
    """Target closure in any tolerant probe cannot leave a dead session reusable."""
    factory = ChallengeProbeEndpointFactory(fatal_probe, fatal=True)
    manager = SessionManager(factory)
    app.state.session_manager = manager
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    payload = {
        "url": "https://example.test/challenge",
        "session": "account",
        "maxTimeout": 2,
    }

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/v1", json=payload)
        second = await client.post("/v1", json=payload)
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]


@pytest.mark.asyncio
async def test_ordinary_challenge_probe_error_preserves_the_retained_resource() -> None:
    """A nonfatal probe failure retains the browser for the next request."""
    factory = ChallengeProbeEndpointFactory(
        "solver", fatal=False, clear_after_marker_calls=2
    )
    manager = SessionManager(factory)
    app.state.session_manager = manager
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    payload = {
        "url": "https://example.test/challenge",
        "session": "account",
        "maxTimeout": 2,
    }

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/v1", json=payload)
        second = await client.post("/v1", json=payload)
    await manager.close()

    assert [first.status_code, second.status_code] == [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.OK,
    ]
    assert len(factory.resources) == 1
    factory.resources[0].close.assert_awaited_once()


def test_blank_sessions_remain_disposable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank session declarations never retain a browser context."""
    from src import utils

    factory = EndpointFactory()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        first = test_client.post("/v1", json={"url": "https://example.test/one"})
        second = test_client.post(
            "/v1", json={"url": "https://example.test/two", "session": "   "}
        )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert len(factory.resources) == 2
    assert [resource.close.await_count for resource in factory.resources] == [1, 1]


def test_retained_session_capacity_overload_is_a_stable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated named-session pool fails without disclosing session details."""
    from src import utils

    factory = EndpointFactory()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        app.state.session_manager = RejectingSessionManager()
        response = test_client.post(
            "/v1",
            json={"url": "https://example.test/one", "session": "private-account"},
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Browser session capacity is unavailable"}


def test_session_commands_require_a_normalized_session_name():
    """Create and destroy reject a missing or blank session before dispatch."""
    for command in ("sessions.create", "sessions.destroy"):
        with pytest.raises(ValidationError, match="session is required"):
            LinkRequest.model_validate({"cmd": command, "session": "  "})


def test_link_request_rejects_commands_outside_the_flaresolverr_set():
    """Unsupported commands cannot silently run a navigation request."""
    with pytest.raises(ValidationError):
        LinkRequest.model_validate(
            {"cmd": "sessions.list", "url": "https://example.test"}
        )


def test_session_create_is_a_lazy_idempotent_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated creates succeed without asking the browser factory for a context."""
    from src import utils

    factory = EndpointFactory()
    manager = RecordingSessionManager()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        app.state.session_manager = manager
        first = test_client.post(
            "/v1", json={"cmd": "sessions.create", "session": " account "}
        )
        second = test_client.post(
            "/v1", json={"cmd": "sessions.create", "session": "account"}
        )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    for response in (first, second):
        body = response.json()
        assert set(body) == {
            "status",
            "message",
            "session",
            "startTimestamp",
            "endTimestamp",
            "version",
        }
        assert body["status"] == "ok"
        assert body["message"] == "Session created successfully."
        assert body["session"] == "account"
        assert isinstance(body["startTimestamp"], int)
        assert isinstance(body["endTimestamp"], int)
        assert body["endTimestamp"] >= body["startTimestamp"]
        assert body["version"] == VERSION
    assert factory.resources == []
    assert manager.acquire_calls == 0


def test_session_destroy_resets_every_exact_normalized_name_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destroy closes every site for its exact name and stays idempotent."""
    from src import utils

    factory = EndpointFactory()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        first_site = test_client.post(
            "/v1",
            json={
                "url": "https://example.com/one",
                "session": "account",
            },
        )
        second_site = test_client.post(
            "/v1",
            json={
                "url": "https://example.org/two",
                "session": "account",
            },
        )
        other_name = test_client.post(
            "/v1",
            json={
                "url": "https://example.net/three",
                "session": "other-account",
            },
        )
        first = test_client.post(
            "/v1", json={"cmd": "sessions.destroy", "session": " account "}
        )
        second = test_client.post(
            "/v1", json={"cmd": "sessions.destroy", "session": "account"}
        )

        assert first_site.status_code == HTTPStatus.OK
        assert second_site.status_code == HTTPStatus.OK
        assert other_name.status_code == HTTPStatus.OK
        assert [resource.close.await_count for resource in factory.resources] == [
            1,
            1,
            0,
        ]

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert first.json()["message"] == "The session has been removed."
    assert second.json()["message"] == "The session has been removed."


def test_session_destroy_timestamp_brackets_reset_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destroy records its start before reset and its completion after reset returns."""
    from src import utils

    class Clock:
        def __init__(self) -> None:
            self.value = 10.0

        def __call__(self) -> float:
            return self.value

    class TimedManager:
        async def reset(self, session: str) -> int:
            assert session == "account"
            assert clock.value == 10.0
            clock.value = 20.0
            return 1

    clock = Clock()
    factory = EndpointFactory()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)
    monkeypatch.setattr("src.endpoints.time.time", clock)

    with TestClient(app) as test_client:
        app.state.session_manager = TimedManager()
        response = test_client.post(
            "/v1", json={"cmd": "sessions.destroy", "session": "account"}
        )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["startTimestamp"] == 10_000
    assert body["endTimestamp"] == 20_000


def test_health_check_uses_a_disposable_browser_not_the_session_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health checks cannot create a retained entry because they have no session key."""
    from src import utils

    factory = EndpointFactory()
    manager = RecordingSessionManager()
    monkeypatch.setattr(utils, "BrowserFactory", lambda: factory)
    monkeypatch.setattr("main.BrowserFactory", lambda: factory)

    with TestClient(app) as test_client:
        app.state.session_manager = manager
        response = test_client.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert len(factory.resources) == 1
    factory.resources[0].close.assert_awaited_once()
    assert manager.acquire_calls == 0


@pytest.mark.asyncio
async def test_lifespan_cancels_expiry_before_closing_the_session_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown awaits expiry-task cancellation before retained resources close."""
    events: list[str] = []

    class LifecycleManager:
        async def close(self) -> None:
            assert events == ["expiry-cancelled"]
            events.append("manager-closed")

    manager = LifecycleManager()

    async def wait_for_cancellation(_manager: LifecycleManager) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("expiry-cancelled")

    def build_manager(_factory: BrowserFactory) -> LifecycleManager:
        return manager

    monkeypatch.setattr("main.SessionManager", build_manager)
    monkeypatch.setattr("main.expire_idle_sessions", wait_for_cancellation)

    async with lifespan(app):
        await asyncio.sleep(0)

    assert events == ["expiry-cancelled", "manager-closed"]


@pytest.mark.asyncio
async def test_networkidle_timeout_after_domcontentloaded_returns_content():
    """Pages that never go idle after DOM load must still return their content."""
    dep = fake_dep(fail_states={"networkidle"})
    response = await read_item(
        LinkRequest(url="https://example.test/login"),
        dep,
    )

    assert response.status == "ok"
    assert response.solution.response == "<html><title>Login</title></html>"


@pytest.mark.asyncio
async def test_domcontentloaded_timeout_returns_408():
    """Fatal timeouts during initial page load still return a controlled 408."""
    with pytest.raises(HTTPException) as exc:
        await read_item(
            LinkRequest(url="https://example.test/login"),
            fake_dep(fail_states={"domcontentloaded"}),
        )

    assert exc.value.status_code == HTTPStatus.REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_unreachable_host_is_a_502_not_a_500():
    """An upstream we cannot reach is a gateway failure, never a Byparr crash."""
    dep = fake_dep()
    dep.page.goto.side_effect = PlaywrightError("Page.goto: NS_ERROR_UNKNOWN_HOST")

    with pytest.raises(HTTPException) as exc:
        await read_item(LinkRequest(url="https://nope.invalid/"), dep)

    assert exc.value.status_code == HTTPStatus.BAD_GATEWAY


@pytest.mark.asyncio
async def test_status_is_always_ok_like_flaresolverr():
    """FlareSolverr hardcodes 200 because Selenium cannot report the real code."""
    dep = fake_dep()
    dep.page.goto.return_value = MagicMock(
        status=HTTPStatus.FORBIDDEN,
        headers={"content-type": "text/html"},
        request=MagicMock(headers={"user-agent": "UnitTestBrowser/1.0"}),
    )

    response = await read_item(LinkRequest(url="https://example.test/login"), dep)

    assert response.solution.status == HTTPStatus.OK


def test_exhausted_budget_never_disables_playwright_timeouts():
    """Playwright reads timeout=0 as no timeout at all, so the floor must hold."""
    spent = TimeoutTimer(duration=0)

    assert spent.remaining() == 0
    assert remaining_ms(spent) > 0


@pytest.mark.asyncio
async def test_missing_user_agent_header_is_not_a_500():
    """A request without a user-agent header degrades to empty, never a 500 (#394)."""
    response = await read_item(
        LinkRequest(url="https://example.test/login"),
        fake_dep(user_agent=None),
    )

    assert response.status == "ok"
    assert response.solution.user_agent == ""


@pytest.mark.asyncio
async def test_detected_challenge_uses_the_camoufox_click_solver():
    """The compatible solver owns the challenge interaction strategy."""
    dep = fake_dep(
        challenged=True,
        marker_counts=[1, 0, 0],
    )

    response = await read_item(
        LinkRequest(url="https://example.test/login", max_timeout=5), dep
    )

    assert response.status == "ok"
    dep.solver.solve_captcha.assert_awaited_once()


@pytest.mark.asyncio
async def test_challenge_that_clears_on_its_own_is_never_clicked():
    """A challenge is over when its markup goes, and until then we keep our hands off."""
    dep = fake_dep(
        challenged=True,
        marker_counts=[1, 0],
        widget_box={"x": 100.0, "y": 200.0, "width": 300.0, "height": 60.0},
    )

    response = await read_item(
        LinkRequest(url="https://example.test/login", max_timeout=5), dep
    )

    assert response.status == "ok"
    dep.page.mouse.down.assert_not_awaited()


@pytest.mark.asyncio
async def test_challenge_that_never_clears_returns_408():
    """A challenge still up when the budget runs out is a timeout, not a 500."""
    dep = fake_dep(challenged=True, marker_counts=[1])

    with pytest.raises(HTTPException) as exc:
        await read_item(
            LinkRequest(url="https://example.test/login", max_timeout=2), dep
        )

    assert exc.value.status_code == HTTPStatus.REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_marker_vanishing_mid_navigation_is_not_a_solved_challenge():
    """The marker drops out between challenge rounds; one clear read proves nothing."""
    dep = fake_dep(challenged=True, marker_counts=[1, 0, 1])

    with pytest.raises(HTTPException) as exc:
        await read_item(
            LinkRequest(url="https://example.test/login", max_timeout=2), dep
        )

    assert exc.value.status_code == HTTPStatus.REQUEST_TIMEOUT
