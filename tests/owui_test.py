import logging
from http import HTTPStatus
from io import StringIO
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from starlette.testclient import TestClient

from main import app
from src.owui import LoadRequest, load_urls
from src.utils import BrowserDepClass
from src.utils import logger as production_logger

client = TestClient(app)


def test_owui_load_basic():
    """/load returns one result per URL with the expected shape."""
    response = client.post("/load", json={"urls": ["https://example.com"]})
    assert response.status_code == HTTPStatus.OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["page_content"]
    assert results[0]["metadata"] == {"source": "https://example.com"}


def test_owui_load_multiple_urls():
    """/load returns one result per URL, in order."""
    urls = ["https://example.com", "https://example.org"]
    response = client.post("/load", json={"urls": urls})
    assert response.status_code == HTTPStatus.OK
    results = response.json()
    assert [r["metadata"]["source"] for r in results] == urls


def test_owui_load_invalid_url_graceful():
    """Unreachable URLs yield empty page_content instead of an error."""
    response = client.post(
        "/load", json={"urls": ["https://this-domain-does-not-exist-12345.invalid"]}
    )
    assert response.status_code == HTTPStatus.OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["page_content"] == ""


@pytest.mark.parametrize(
    "headers",
    [None, {"Authorization": "Bearer wrong-key"}],
)
def test_owui_load_rejects_missing_or_wrong_key(headers):
    """/load returns 401 without a valid bearer token when a key is set."""
    with patch("src.owui.OWUI_API_KEY", "test-secret-key"):
        response = client.post(
            "/load", json={"urls": ["https://example.com"]}, headers=headers
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_owui_load_accepts_valid_key():
    """/load succeeds with the configured bearer token."""
    with patch("src.owui.OWUI_API_KEY", "test-secret-key"):
        response = client.post(
            "/load",
            json={"urls": ["https://example.com"]},
            headers={"Authorization": "Bearer test-secret-key"},
        )
        assert response.status_code == HTTPStatus.OK


ARTICLE_HTML = """<html><head><title>Test</title></head><body>
<article><h1>Example Title</h1><p>This is the main article body with enough words for
trafilatura to consider it real content rather than boilerplate.</p></article>
<nav><a href="/x">nav link</a></nav>
</body></html>"""


def fake_dep(*, html: str = ARTICLE_HTML) -> BrowserDepClass:
    """Browser dependency whose page loads HTML but never reaches networkidle."""
    page = AsyncMock()
    page.goto.return_value = MagicMock()
    page.content.return_value = html
    page.locator = MagicMock()
    page.locator.return_value.inner_text = AsyncMock(
        return_value="line one\n\nline two"
    )

    def wait_for_load_state(state: str, **_kwargs: object) -> None:
        if state == "networkidle":
            message = "load state wait timed out"
            raise PlaywrightTimeoutError(message)

    page.wait_for_load_state.side_effect = wait_for_load_state
    return BrowserDepClass(page=page, context=AsyncMock(), solver=AsyncMock())


@pytest.mark.asyncio
async def test_networkidle_timeout_still_extracts_content():
    """A page that never reaches networkidle still yields its article text."""
    results = await load_urls(
        LoadRequest(urls=["https://example.test"]), None, fake_dep()
    )
    assert results[0].page_content == (
        "Example TitleThis is the main article body with enough words for "
        "trafilatura to consider it real content rather than boilerplate."
    )


@pytest.mark.asyncio
async def test_extraction_falls_back_to_innertext():
    """Pages trafilatura cannot score fall back to the rendered innerText."""
    results = await load_urls(
        LoadRequest(urls=["https://example.test"]),
        None,
        fake_dep(html="<html><body></body></html>"),
    )
    assert results[0].page_content == "line one\nline two"


@pytest.mark.asyncio
async def test_load_browser_logs_render_safe_sites_without_raw_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout and failure diagnostics cannot serialize caller or browser secrets."""
    timeout_url = (
        "https://timeout-user:timeout-password@reader.example.com/private/timeout"
        "?token=timeout-query-secret"
    )
    failure_url = (
        "https://failure-user:failure-password@reader.example.org/private/failure"
        "?token=failure-query-secret"
    )
    scoped_ipv6_url = (
        "https://scope-user:scope-password@"
        "[fe80::2%outcome=success session=load-scope-secret]"
        "/private/scoped?token=scoped-query-secret"
    )
    leaked_values = (
        timeout_url,
        failure_url,
        scoped_ipv6_url,
        "/private/timeout",
        "/private/failure",
        "timeout-user",
        "timeout-password",
        "timeout-query-secret",
        "failure-user",
        "failure-password",
        "failure-query-secret",
        "scope-user",
        "scope-password",
        "load-scope-secret",
        "scoped-query-secret",
        "cookie-secret",
        "header-secret",
    )
    dep = fake_dep()
    page = cast("AsyncMock", dep.page)
    page.goto.side_effect = [
        None,
        PlaywrightError(" | ".join(leaked_values)),
        PlaywrightError(" | ".join(leaked_values)),
    ]
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    previous_level = production_logger.level
    production_logger.setLevel(logging.DEBUG)
    try:
        results = await load_urls(
            LoadRequest(urls=[timeout_url, failure_url, scoped_ipv6_url]),
            None,
            dep,
        )
    finally:
        production_logger.setLevel(previous_level)

    assert results[0].page_content
    assert results[1].page_content == ""
    assert results[2].page_content == ""
    rendered = stream.getvalue()
    assert (
        "owui_load_networkidle_timeout site=example.com error_type=TimeoutError"
        in rendered
    )
    assert "owui_load_error site=example.org error_type=Error" in rendered
    assert "owui_load_error site=fe80::2 error_type=Error" in rendered
    for secret in leaked_values:
        assert secret not in rendered
