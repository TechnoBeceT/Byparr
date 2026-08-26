# ruff: noqa: D103

from collections.abc import AsyncGenerator
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from main import app
from src.utils import BrowserDepClass, get_browser

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def override_browser_dependency() -> None:
    async def browser_dependency() -> AsyncGenerator[BrowserDepClass]:
        yield BrowserDepClass(page=AsyncMock(), context=AsyncMock())

    app.dependency_overrides[get_browser] = browser_dependency
    yield
    app.dependency_overrides.pop(get_browser, None)


@pytest.mark.parametrize("session", [1, [], {}])
def test_invalid_session_types_return_422_from_the_endpoint(session: object) -> None:
    response = client.post(
        "/v1", json={"url": "https://example.com", "session": session}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT


def test_malformed_json_returns_422_from_the_endpoint() -> None:
    response = client.post(
        "/v1",
        content='{"url": "https://example.com", "session":',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
