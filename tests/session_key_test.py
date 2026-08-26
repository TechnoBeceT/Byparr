# ruff: noqa: D103, S106, S603

import subprocess
import sys

import pytest
from pydantic import ValidationError

from src.models import LinkRequest
from src.proxy import ProxySettings, resolve_proxy_settings
from src.session_key import build_session_key


def test_link_request_normalizes_a_session_name() -> None:
    request = LinkRequest(url="https://example.com", session="  account-a  ")

    assert request.session == "account-a"


@pytest.mark.parametrize("session", ["", "   "])
def test_link_request_treats_blank_session_as_omitted(session: str) -> None:
    request = LinkRequest(url="https://example.com", session=session)

    assert request.session is None


@pytest.mark.parametrize("session", ["bad\nname", "bad\tname", "bad\x00name"])
def test_link_request_rejects_non_printable_session_names(session: str) -> None:
    with pytest.raises(ValidationError, match="printable"):
        LinkRequest(url="https://example.com", session=session)


def test_link_request_rejects_sessions_longer_than_128_characters() -> None:
    with pytest.raises(ValidationError):
        LinkRequest(url="https://example.com", session="s" * 129)


@pytest.mark.parametrize("session", [1, [], {}])
def test_link_request_rejects_non_string_session_values(session: object) -> None:
    with pytest.raises(ValidationError):
        LinkRequest(url="https://example.com", session=session)


def test_session_key_uses_the_registrable_domain() -> None:
    proxy = ProxySettings.direct()

    left = build_session_key(
        "account-a", "https://reader.manga.example.co.uk/chapter/1", proxy
    )
    right = build_session_key("account-a", "https://api.example.co.uk/catalog", proxy)

    assert left == right
    assert left is not None
    assert left.site == "example.co.uk"


def test_session_key_isolated_by_site() -> None:
    proxy = ProxySettings.direct()

    left = build_session_key("account-a", "https://example.com", proxy)
    right = build_session_key("account-a", "https://example.net", proxy)

    assert left != right


@pytest.mark.parametrize("platform", ["pages.dev", "netlify.app", "vercel.app"])
def test_session_key_isolates_private_suffix_tenants(platform: str) -> None:
    first = build_session_key(
        "account-a", f"https://first-tenant.{platform}", ProxySettings.direct()
    )
    second = build_session_key(
        "account-a", f"https://second-tenant.{platform}", ProxySettings.direct()
    )

    assert first != second
    assert first is not None
    assert first.site == f"first-tenant.{platform}"


def test_session_key_uses_uts46_idna_canonicalization() -> None:
    proxy = ProxySettings.direct()

    unicode_key = build_session_key("account-a", "https://faß.de", proxy)
    punycode_key = build_session_key("account-a", "https://xn--fa-hia.de", proxy)
    ascii_key = build_session_key("account-a", "https://fass.de", proxy)

    assert unicode_key == punycode_key
    assert unicode_key != ascii_key


def test_session_key_isolated_by_proxy_egress() -> None:
    direct = build_session_key(
        "account-a", "https://example.com", ProxySettings.direct()
    )
    first_proxy = build_session_key(
        "account-a",
        "https://example.com",
        ProxySettings(
            server="http://proxy-one.test:8080", username="alice", password="secret-one"
        ),
    )
    second_proxy = build_session_key(
        "account-a",
        "https://example.com",
        ProxySettings(
            server="http://proxy-two.test:8080", username="alice", password="secret-two"
        ),
    )

    assert direct != first_proxy
    assert first_proxy != second_proxy


def test_proxy_identity_and_repr_do_not_expose_credentials() -> None:
    proxy = ProxySettings(
        server="http://proxy.test:8080",
        username="alice",
        password="private-password",
    )

    assert "alice" not in proxy.identity
    assert "private-password" not in proxy.identity
    assert "alice" not in repr(proxy)
    assert "private-password" not in repr(proxy)


def test_proxy_identity_is_process_private_and_stable_per_process() -> None:
    code = (
        "from src.proxy import ProxySettings; "
        "print(ProxySettings('http://proxy.test:8080', 'alice', 'private-password').identity)"
    )
    first_process = subprocess.check_output(
        [sys.executable, "-c", code], text=True
    ).strip()
    second_process = subprocess.check_output(
        [sys.executable, "-c", code], text=True
    ).strip()
    local_identity = ProxySettings(
        "http://proxy.test:8080", "alice", "private-password"
    ).identity

    assert first_process != second_process
    assert (
        local_identity
        == ProxySettings("http://proxy.test:8080", "alice", "private-password").identity
    )


def test_header_proxy_overrides_environment_proxy() -> None:
    proxy = resolve_proxy_settings(
        header_server="http://header-proxy.test:8080",
        header_username="header-user",
        header_password="header-password",
        environment_server="http://environment-proxy.test:8080",
        environment_username="environment-user",
        environment_password="environment-password",
    )

    assert proxy.as_playwright_proxy() == {
        "server": "http://header-proxy.test:8080",
        "username": "header-user",
        "password": "header-password",
    }


def test_environment_proxy_is_used_when_proxy_header_is_omitted() -> None:
    proxy = resolve_proxy_settings(
        header_server=None,
        header_username="ignored-header-user",
        header_password="ignored-header-password",
        environment_server="http://environment-proxy.test:8080",
        environment_username="environment-user",
        environment_password="environment-password",
    )

    assert proxy.as_playwright_proxy() == {
        "server": "http://environment-proxy.test:8080",
        "username": "environment-user",
        "password": "environment-password",
    }


def test_whitespace_proxy_header_falls_back_to_environment_proxy() -> None:
    proxy = resolve_proxy_settings(
        header_server="  \t ",
        header_username="ignored-header-user",
        header_password="ignored-header-password",
        environment_server="http://environment-proxy.test:8080",
        environment_username="environment-user",
        environment_password="environment-password",
    )

    assert proxy.as_playwright_proxy() == {
        "server": "http://environment-proxy.test:8080",
        "username": "environment-user",
        "password": "environment-password",
    }


def test_whitespace_environment_proxy_uses_direct_egress() -> None:
    proxy = resolve_proxy_settings(
        header_server=None,
        header_username=None,
        header_password=None,
        environment_server="\t ",
        environment_username="environment-user",
        environment_password="environment-password",
    )

    assert proxy == ProxySettings.direct()


def test_omitted_session_has_no_session_key() -> None:
    assert (
        build_session_key(None, "https://example.com", ProxySettings.direct()) is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "example.com",
        "ftp://example.com",
        "https:///missing-host",
        "https://.",
        "https://a..example.com",
    ],
)
def test_session_key_rejects_urls_without_absolute_http_scheme(url: str) -> None:
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        build_session_key("account-a", url, ProxySettings.direct())
