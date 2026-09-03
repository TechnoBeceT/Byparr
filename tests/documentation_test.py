from pathlib import Path

from src.browser import BrowserFactory

PROJECT_ROOT = Path(__file__).parents[1]


def test_public_documentation_matches_direct_only_browser_contract() -> None:
    """Public guidance must not advertise unsupported proxy behavior."""
    readme = (PROJECT_ROOT / "README.md").read_text()
    rendered_text = " ".join(readme.split())

    assert (
        "`PROXY_SERVER`, `PROXY_USERNAME`, and `PROXY_PASSWORD` remain accepted "
        "only for configuration compatibility." in rendered_text
    )
    assert (
        "Proxied browser launches are unsupported and fail before browser startup."
        in rendered_text
    )
    assert "Direct browser launches default to `en-US`." in rendered_text
    assert BrowserFactory.__doc__ == (
        "Open direct Camoufox resources and reject proxies before browser startup."
    )

    stale_claims = (
        "Proxy Recommendation",
        "ProxyBase",
        "affiliate",
        "egress country",
        "exit IP",
        "French proxy",
        "work seamlessly with Byparr",
        "improve your success rate",
    )
    assert not any(claim.lower() in rendered_text.lower() for claim in stale_claims)
