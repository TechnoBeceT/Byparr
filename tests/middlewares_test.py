# ruff: noqa: D103

from collections.abc import AsyncGenerator
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.utils import BrowserDepClass, get_browser


@pytest.fixture(autouse=True)
def override_browser_dependency() -> None:
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
