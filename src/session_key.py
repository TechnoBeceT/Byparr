"""Session partitioning helpers for browser contexts."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import idna
import tldextract

from src.proxy import ProxySettings

INVALID_SESSION_URL_MESSAGE = "session URLs must be absolute HTTP(S) URLs"
_PUBLIC_SUFFIX_EXTRACTOR = tldextract.TLDExtract(
    suffix_list_urls=(), include_psl_private_domains=True
)


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

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError(INVALID_SESSION_URL_MESSAGE) from exc
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
    try:
        normalized = idna.encode(
            hostname.rstrip("."), uts46=True, transitional=False
        ).decode("ascii")
    except idna.IDNAError as exc:
        raise ValueError(INVALID_SESSION_URL_MESSAGE) from exc
    if not normalized:
        raise ValueError(INVALID_SESSION_URL_MESSAGE)
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        extracted = _PUBLIC_SUFFIX_EXTRACTOR(normalized)
        return extracted.top_domain_under_public_suffix or normalized
