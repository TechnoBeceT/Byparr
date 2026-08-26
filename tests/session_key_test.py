# ruff: noqa: D103, S106, S603

import subprocess
import sys

import pytest
from pydantic import ValidationError

from src.models import LinkRequest
from src.proxy import ProxySettings, resolve_proxy_settings
from src.session_key import build_session_key, safe_site_label


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
        LinkRequest.model_validate({"url": "https://example.com", "session": session})


def test_session_key_uses_the_registrable_domain() -> None:
    proxy = ProxySettings.direct()

    left = build_session_key(
        "account-a", "https://reader.manga.example.co.uk/chapter/1", proxy
    )
    right = build_session_key("account-a", "https://api.example.co.uk/catalog", proxy)

    assert left == right
    assert left is not None
    assert left.site == "example.co.uk"


@pytest.mark.parametrize("separator", [".", "\u3002", "\uff0e", "\uff61"])
def test_session_key_accepts_one_trailing_dns_root_separator(separator: str) -> None:
    proxy = ProxySettings.direct()

    dotted = build_session_key(
        "account-a", f"https://reader.example.com{separator}", proxy
    )
    plain = build_session_key("account-a", "https://reader.example.com", proxy)

    assert dotted == plain


@pytest.mark.parametrize(
    "separators",
    [
        "..",
        "...",
        "\u3002.",
        ".\u3002",
        "\u3002\u3002",
        "\uff0e.",
        ".\uff0e",
        "\uff0e\uff0e",
        "\uff61.",
        ".\uff61",
        "\uff61\uff61",
    ],
)
def test_session_key_rejects_multiple_trailing_dns_root_separators(
    separators: str,
) -> None:
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        build_session_key(
            "account-a", f"https://example.com{separators}", ProxySettings.direct()
        )


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


@pytest.mark.parametrize(
    ("url", "expected_site"),
    [
        ("https://127.0.0.1/chapter/1", "127.0.0.1"),
        ("https://[::1]/chapter/1", "::1"),
        ("https://[2001:0db8::1]/chapter/1", "2001:db8::1"),
    ],
)
def test_session_key_normalizes_ip_literal_sites(url: str, expected_site: str) -> None:
    key = build_session_key("account-a", url, ProxySettings.direct())

    assert key is not None
    assert key.site == expected_site


@pytest.mark.parametrize(
    ("url", "expected_site"),
    [
        ("https://[fe80::1%25scope-secret]/private?token=query", "fe80::1"),
        ("https://[FE80:0:0::1%raw-scope-secret]/private", "fe80::1"),
        (
            (
                "https://user:password@"
                "[fe80::2%outcome=success session=scope-secret]"
                "/private?token=query-secret"
            ),
            "fe80::2",
        ),
        ("https://[2001:0db8::1]/chapter/1", "2001:db8::1"),
        ("https://192.0.2.1/private?token=query", "192.0.2.1"),
        (
            "https://user:password@reader.example.com/private?token=query",
            "example.com",
        ),
    ],
)
def test_safe_site_label_canonicalizes_without_ipv6_zone_or_url_secrets(
    url: str,
    expected_site: str,
) -> None:
    assert safe_site_label(url) == expected_site


@pytest.mark.parametrize(
    "url",
    [
        "https://[fe80::gg%scope-secret]/private",
        "https://[fe80::1%]/private",
        "https://[fe80::1/private",
        "https://[fe80::1%25outcome%3Dsuccess%20session%3Dscope-secret]/private",
    ],
)
def test_safe_site_label_rejects_malformed_ipv6_without_exposing_input(
    url: str,
) -> None:
    assert safe_site_label(url) == "invalid-target"


def test_scoped_ipv6_session_partitioning_remains_zone_sensitive() -> None:
    proxy = ProxySettings.direct()

    first = build_session_key("account-a", "https://[fe80::1%25zone-a]", proxy)
    second = build_session_key("account-a", "https://[fe80::1%25zone-b]", proxy)

    assert first != second


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


@pytest.mark.parametrize(
    "url",
    [
        r"https://evil.example\@victim.example",
        r"https://victim.example\chapter/1",
        r"https://victim.example/chapter\1",
    ],
)
def test_session_key_rejects_raw_backslashes_anywhere_in_the_url(url: str) -> None:
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        build_session_key("account-a", url, ProxySettings.direct())
