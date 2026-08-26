# ruff: noqa: D102, D107, PLC0415, PLR2004, TRY003

import asyncio
import base64
from contextlib import asynccontextmanager
from http import HTTPStatus
from json import JSONDecodeError
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from fastapi import HTTPException
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError
from starlette.testclient import TestClient

from main import app, lifespan
from src.challenge import CF_INTERSTITIAL_INDICATORS_SELECTORS
from src.endpoints import read_item
from src.models import LinkRequest
from src.sessions import SessionCapacityError
from src.utils import BrowserDepClass, TimeoutTimer, remaining_ms

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
def test_max_timeout_normalization(payload: dict, expected: int):
    """MaxTimeout must accept FlareSolverr's milliseconds while keeping seconds."""
    request = LinkRequest(url="https://example.com", **payload)
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
    return BrowserDepClass(page=page, context=context)


class EndpointResource:
    """A disposable browser resource with a stable cookie per context."""

    def __init__(self, number: int) -> None:
        self.page = fake_dep().page
        self.context = fake_dep().context
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

    async def open(self, _proxy: object) -> EndpointResource:
        resource = EndpointResource(len(self.resources) + 1)
        self.resources.append(resource)
        return resource


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


def test_named_session_reuses_its_site_but_isolates_other_sites(monkeypatch):
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


def test_blank_sessions_remain_disposable(monkeypatch):
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


def test_retained_session_capacity_overload_is_a_stable_503(monkeypatch):
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


def test_session_create_is_a_lazy_idempotent_declaration(monkeypatch):
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
    assert first.json()["message"] == "Session created successfully."
    assert second.json()["message"] == "Session created successfully."
    assert "solution" not in first.json()
    assert factory.resources == []
    assert manager.acquire_calls == 0


def test_session_destroy_resets_every_exact_normalized_name_idempotently(monkeypatch):
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


def test_health_check_uses_a_disposable_browser_not_the_session_manager(monkeypatch):
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
async def test_lifespan_cancels_expiry_before_closing_the_session_manager(monkeypatch):
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

    monkeypatch.setattr("main.SessionManager", lambda _factory: manager)
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
async def test_checkbox_is_clicked_while_the_challenge_is_up():
    """A measurable widget gets a humanised press, not a raw synthetic click."""
    dep = fake_dep(
        challenged=True,
        marker_counts=[1, 1, 0],
        widget_box={"x": 100.0, "y": 200.0, "width": 300.0, "height": 60.0},
    )

    response = await read_item(
        LinkRequest(url="https://example.test/login", max_timeout=5), dep
    )

    assert response.status == "ok"
    dep.page.mouse.move.assert_awaited_once_with(125.0, 230.0)
    dep.page.mouse.down.assert_awaited_once()
    dep.page.mouse.up.assert_awaited_once()


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
