import asyncio
import time
import warnings
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.browser import BrowserResourceUnusableError, is_fatal_browser_error
from src.challenge import ChallengeSolverError, challenge_present, solve_challenge
from src.content import build_response_content
from src.models import (
    HealthcheckResponse,
    LinkRequest,
    LinkResponse,
    SessionResponse,
    Solution,
)
from src.utils import (
    BrowserDepClass,
    TimeoutTimer,
    get_browser,
    get_request_browser,
    log_browser_error,
    logger,
    remaining_ms,
)

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

BrowserDep = Annotated[BrowserDepClass, Depends(get_browser)]


@router.get("/", include_in_schema=False)
def read_root():
    """Redirect to /docs."""
    logger.debug("Redirecting to /docs")
    return RedirectResponse(url="/docs", status_code=301)


@router.get("/health")
async def health_check(sb: BrowserDep):
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
        sb,
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


async def read_item(request: LinkRequest, dep: BrowserDepClass) -> LinkResponse:
    """Navigate one browser resource and return the FlareSolverr response."""
    start_time = int(time.time() * 1000)
    timer = TimeoutTimer(duration=request.max_timeout)
    request.url = request.url.replace('"', "").strip()

    async with setup_routes(request, dep):
        try:
            challenge_detected, page_html, page_request = await _navigate_and_solve(
                dep, request, timer
            )
        except (TimeoutError, PlaywrightTimeoutError) as error:
            log_browser_error(
                "browser_request_error", request.url, error, outcome="timeout"
            )
            raise HTTPException(
                status_code=408,
                detail="Timed out while loading the page or solving the challenge",
            ) from error
        except PlaywrightError as error:
            if is_fatal_browser_error(error):
                raise
            log_browser_error(
                "browser_request_error", request.url, error, outcome="failure"
            )
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach the target ({type(error).__name__})",
            ) from error
        except ChallengeSolverError as error:
            log_browser_error(
                "browser_request_error", request.url, error, outcome="solver_failure"
            )
            raise HTTPException(
                status_code=502,
                detail="The challenge solver could not complete",
            ) from error

        cookies = await dep.context.cookies()
        content_type, response_content = await build_response_content(
            dep.page,
            request,
            page_request,
            challenge_detected=challenge_detected,
            page_html=page_html,
        )

        user_agent = (
            page_request.request.headers.get("user-agent") or "" if page_request else ""
        )

        return LinkResponse(
            message="Success",
            solution=Solution(
                user_agent=user_agent,
                url=dep.page.url,
                status=HTTPStatus.OK,
                cookies=cookies,
                headers=page_request.headers if page_request else {},
                response=response_content,
                content_type=content_type,
            ),
            start_timestamp=start_time,
        )


@router.post("/v1", response_model_exclude_none=True)
async def handle_v1(
    request: LinkRequest,
    app_request: Request,
    x_proxy_server: Annotated[str | None, Header(alias="X-Proxy-Server")] = None,
    x_proxy_username: Annotated[str | None, Header(alias="X-Proxy-Username")] = None,
    x_proxy_password: Annotated[str | None, Header(alias="X-Proxy-Password")] = None,
) -> LinkResponse | SessionResponse:
    """Select the request browser lifecycle before running navigation."""
    start_time = int(time.time() * 1000)
    if request.cmd == "sessions.create":
        assert request.session is not None
        return SessionResponse(
            message="Session created successfully.",
            session=request.session,
            start_timestamp=start_time,
        )
    if request.cmd == "sessions.destroy":
        assert request.session is not None
        await app_request.app.state.session_manager.reset(request.session)
        return SessionResponse(
            message="The session has been removed.", start_timestamp=start_time
        )
    try:
        async with get_request_browser(
            request,
            getattr(app_request.app.state, "session_manager", None),
            x_proxy_server=x_proxy_server,
            x_proxy_username=x_proxy_username,
            x_proxy_password=x_proxy_password,
        ) as dep:
            return await read_item(request, dep)
    except HTTPException:
        raise
    except PlaywrightError as error:
        log_browser_error(
            "browser_request_error", request.url, error, outcome="failure"
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the target ({type(error).__name__})",
        ) from error
    except BaseException as error:
        if not is_fatal_browser_error(error):
            raise
        log_browser_error("browser_request_error", request.url, error, outcome="closed")
        raise HTTPException(
            status_code=502,
            detail="The browser resource closed while handling the request",
        ) from error


@asynccontextmanager
async def setup_routes(
    request: LinkRequest, dep: BrowserDepClass
) -> AsyncGenerator[None]:
    """Own the media-blocking route for exactly one request."""
    if not request.block_media:
        yield
        return

    async def block_media_route(route: Route) -> None:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    registration_cancelled, registration_error = await _complete_route_operation(
        dep.page.route("**/*", block_media_route)
    )
    if registration_error is not None or registration_cancelled:
        try:
            cleanup_cancelled = await _remove_owned_route(dep.page, block_media_route)
        except BrowserResourceUnusableError as cleanup_error:
            if registration_cancelled:
                raise asyncio.CancelledError from cleanup_error
            raise
        if registration_cancelled or cleanup_cancelled:
            if registration_error is not None and is_fatal_browser_error(
                registration_error
            ):
                raise asyncio.CancelledError from BrowserResourceUnusableError()
            raise asyncio.CancelledError from None
        assert registration_error is not None
        raise registration_error
    try:
        yield
    finally:
        cleanup_cancelled = await _remove_owned_route(dep.page, block_media_route)
        if cleanup_cancelled:
            raise asyncio.CancelledError


async def _remove_owned_route(
    page: Page, handler: Callable[[Route], Awaitable[None]]
) -> bool:
    """Remove the exact request handler or mark the browser state unusable."""
    cancellation_seen, operation_error = await _complete_route_operation(
        page.unroute("**/*", handler)
    )
    if operation_error is not None:
        if cancellation_seen:
            raise asyncio.CancelledError from BrowserResourceUnusableError()
        raise BrowserResourceUnusableError from None
    return cancellation_seen


async def _complete_route_operation(
    operation: Awaitable[object],
) -> tuple[bool, BaseException | None]:
    """Drain one owned operation, consuming its result after caller cancellation."""
    task = asyncio.ensure_future(operation)
    cancellation_seen = False
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            cancellation_seen = True
    try:
        task.result()
    except BaseException as error:
        return cancellation_seen, error
    return cancellation_seen, None


async def _navigate_and_solve(
    dep: BrowserDep,
    request: LinkRequest,
    timer: TimeoutTimer,
) -> tuple[bool, str | None, object]:
    """Navigate to the URL, then solve a challenge or wait for network idle."""
    page_html: str | None = None
    page_request = await dep.page.goto(request.url, timeout=remaining_ms(timer))
    await dep.page.wait_for_load_state(
        state="domcontentloaded", timeout=remaining_ms(timer)
    )

    if not await challenge_present(dep.page):
        page_html = await dep.page.content()
        await _wait_for_networkidle(dep, timer)
        return False, page_html, page_request

    await solve_challenge(dep.page, dep.solver, timer)
    await _wait_for_networkidle(dep, timer)
    return True, page_html, page_request


async def _wait_for_networkidle(dep: BrowserDep, timer: TimeoutTimer) -> None:
    """Wait for network idle, tolerating post-DOM-load stalls."""
    try:
        await dep.page.wait_for_load_state("networkidle", timeout=remaining_ms(timer))
    except PlaywrightTimeoutError:
        logger.info(
            "networkidle timed out after domcontentloaded; continuing with loaded page"
        )
