"""Session partitioning helpers for browser contexts."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from publicsuffix2 import get_sld

from src.proxy import ProxySettings

INVALID_SESSION_URL_MESSAGE = "session URLs must be absolute HTTP(S) URLs"


@dataclass(frozen=True)
class SessionKey:
    """An immutable browser-session identity scoped to a site and egress."""

    session: str
    site: str
    proxy_id: str


def build_session_key(
    session: str | None, url: str, proxy: ProxySettings
) -> SessionKey | None:
    """Build a site- and proxy-isolated key for an optional session name."""
    if session is None:
        return None

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(INVALID_SESSION_URL_MESSAGE)
    try:
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(INVALID_SESSION_URL_MESSAGE) from exc
    if hostname is None:
        raise ValueError(INVALID_SESSION_URL_MESSAGE)

    site = _session_site(hostname)
    return SessionKey(session=session, site=site, proxy_id=proxy.identity)


def _session_site(hostname: str) -> str:
    """Return the registrable domain, or a normalized IP address for IP hosts."""
    normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        return get_sld(normalized, strict=False) or normalized
