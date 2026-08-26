# ruff: noqa: D103

import logging
from collections.abc import AsyncGenerator, Generator
from http import HTTPStatus
from io import StringIO
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from main import app
from src.middlewares import LogRequest
from src.utils import BrowserDepClass, get_browser
from src.utils import logger as production_logger


@pytest.fixture(autouse=True)
def override_browser_dependency() -> Generator[None]:
    async def browser_dependency() -> AsyncGenerator[BrowserDepClass]:
        yield BrowserDepClass(page=AsyncMock(), context=AsyncMock())

    app.dependency_overrides[get_browser] = browser_dependency
    yield
    app.dependency_overrides.pop(get_browser, None)


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        yield async_client


@pytest.mark.parametrize("session", [1, [], {}])
@pytest.mark.asyncio
async def test_invalid_session_types_return_422_from_the_endpoint(
    session: object, client: AsyncClient
) -> None:
    response = await client.post(
        "/v1", json={"url": "https://example.com", "session": session}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_malformed_json_returns_422_from_the_endpoint(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1",
        content='{"url": "https://example.com", "session":',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_invalid_utf8_returns_the_framework_body_parse_error(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1",
        content=b'{"url":"https://example.com","session":"\xff"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": "There was an error parsing the body"}


@pytest.mark.asyncio
async def test_solve_logs_use_sanitized_site_context_without_request_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = FastAPI()

    async def solve() -> dict[str, str]:
        return {"status": "ok"}

    target.add_api_route("/v1", solve, methods=["POST"])
    target.add_middleware(LogRequest)
    transport = ASGITransport(app=target)
    full_url = (
        "https://target-user:target-password@reader.example.com/private/path"
        "?token=clearance-secret"
    )
    timeout_seconds = 7
    payload = {
        "url": full_url,
        "session": "session-super-secret",
        "maxTimeout": timeout_seconds * 1000,
    }

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as local_client:
            response = await local_client.post(
                "/v1",
                json=payload,
                headers={
                    "Cookie": "cf_clearance=cookie-super-secret",
                    "X-Proxy-Password": "proxy-super-secret",
                },
            )

    assert response.status_code == HTTPStatus.OK
    solve_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("browser_solve ")
    ]
    fields = [record.__dict__ for record in solve_records]
    assert [field["event"] for field in fields] == [
        "solve_start",
        "solve_finish",
    ]
    assert all(field["site"] == "example.com" for field in fields)
    assert all(field["timeout_seconds"] == timeout_seconds for field in fields)
    assert all(field["session_mode"] == "retained" for field in fields)
    assert fields[-1]["outcome"] == "success"
    duration_ms = fields[-1]["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0

    serialized = "\n".join(str(record.__dict__) for record in caplog.records)
    for secret in (
        full_url,
        "/private/path",
        "target-user",
        "target-password",
        "clearance-secret",
        "session-super-secret",
        "cookie-super-secret",
        "proxy-super-secret",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("log_level", [logging.INFO, logging.DEBUG])
async def test_production_text_logger_renders_safe_solve_context(
    monkeypatch: pytest.MonkeyPatch,
    log_level: int,
) -> None:
    """The shipped formatter receives useful solve fields in the message text."""
    target = FastAPI()

    async def solve() -> dict[str, str]:
        return {"status": "ok"}

    target.add_api_route("/v1", solve, methods=["POST"])
    target.add_middleware(LogRequest)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    monkeypatch.setattr(production_logger, "level", log_level)
    transport = ASGITransport(app=target)

    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as local_client:
        response = await local_client.post(
            "/v1",
            json={
                "url": "https://reader.example.com/private?token=secret",
                "session": "account-secret",
                "maxTimeout": 7000,
            },
        )

    assert response.status_code == HTTPStatus.OK
    rendered = stream.getvalue()
    assert (
        "browser_solve event=solve_start site=example.com timeout_seconds=7 "
        "session_mode=retained" in rendered
    )
    assert (
        "browser_solve event=solve_finish site=example.com timeout_seconds=7"
        in rendered
    )
    assert "outcome=success" in rendered
    assert "duration_ms=" in rendered
    for secret in ("/private", "token=secret", "account-secret"):
        assert secret not in rendered
