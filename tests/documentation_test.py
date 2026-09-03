from pathlib import Path

from src.browser import BrowserFactory

PROJECT_ROOT = Path(__file__).parents[1]


def test_public_documentation_matches_direct_only_browser_contract() -> None:
    """Public guidance must not advertise unsupported proxy behavior."""
    readme = (PROJECT_ROOT / "README.md").read_text()
    rendered_text = " ".join(readme.split())
    agents = " ".join((PROJECT_ROOT / "AGENTS.md").read_text().split())

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
    assert "Every configured proxy fails before Camoufox starts." in agents
    assert (
        "Proxy environment variables and request headers remain parseable for "
        "compatibility, but proxy operation is unsupported." in agents
    )

    stale_claims = (
        "resolved proxy settings",
        "Proxy Recommendation",
        "ProxyBase",
        "affiliate",
        "egress country",
        "exit IP",
        "French proxy",
        "work seamlessly with Byparr",
        "improve your success rate",
    )
    public_docs = (rendered_text, agents)
    assert not any(
        claim.lower() in document.lower()
        for claim in stale_claims
        for document in public_docs
    )
