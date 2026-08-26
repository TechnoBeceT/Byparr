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
    site = _site_from_url(url)
    return SessionKey(session=session, site=site, proxy_id=proxy.identity)


def safe_site_label(url: str) -> str:
    """Return a path- and credential-free site label for operational logs."""
    try:
        return _site_from_url(url)
    except ValueError:
        return "invalid-target"


def _site_from_url(url: str) -> str:
    """Validate an absolute URL and return its normalized site identity."""
    if "\\" in url:
        raise ValueError(INVALID_SESSION_URL_MESSAGE)

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

    return _session_site(hostname)


def _session_site(hostname: str) -> str:
    """Return the registrable domain, or a normalized IP address for IP hosts."""
    ip_candidate = hostname.removesuffix(".")
    try:
        return str(ipaddress.ip_address(ip_candidate))
    except ValueError:
        pass
    try:
        normalized = idna.encode(hostname, uts46=True, transitional=False).decode(
            "ascii"
        )
    except idna.IDNAError as exc:
        raise ValueError(INVALID_SESSION_URL_MESSAGE) from exc
    candidate = normalized.removesuffix(".")
    if not candidate or candidate.endswith("."):
        raise ValueError(INVALID_SESSION_URL_MESSAGE)
    extracted = _PUBLIC_SUFFIX_EXTRACTOR(candidate)
    return extracted.top_domain_under_public_suffix or candidate
