"""Proxy request settings and safe proxy identities."""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from hashlib import sha256

_PROXY_IDENTITY_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, repr=False)
class ProxySettings:
    """The proxy credentials selected for one request."""

    server: str | None = None
    username: str | None = None
    password: str | None = None

    @classmethod
    def direct(cls) -> ProxySettings:
        """Return settings for a direct, non-proxied connection."""
        return cls()

    @property
    def identity(self) -> str:
        """
        Return a process-stable, secret-safe identifier for the selected egress.

        Credential changes deliberately isolate browser state because some proxy
        providers encode egress routing in their credentials.
        """
        if self.server is None:
            return "direct"
        material = json.dumps(
            [self.server, self.username, self.password], separators=(",", ":")
        ).encode()
        digest = hmac.new(_PROXY_IDENTITY_KEY, material, sha256).hexdigest()
        return f"proxy:{digest}"

    def as_playwright_proxy(self) -> dict[str, str | None] | None:
        """Return the proxy format accepted by Camoufox."""
        if self.server is None:
            return None
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }

    def __repr__(self) -> str:
        """Avoid exposing proxy credentials through diagnostic representations."""
        return f"ProxySettings(identity={self.identity!r})"


def resolve_proxy_settings(
    *,
    header_server: str | None,
    header_username: str | None,
    header_password: str | None,
    environment_server: str | None,
    environment_username: str | None,
    environment_password: str | None,
) -> ProxySettings:
    """Resolve a header proxy before the configured environment proxy."""
    selected_header_server = _proxy_selector(header_server)
    if selected_header_server:
        return ProxySettings(selected_header_server, header_username, header_password)
    selected_environment_server = _proxy_selector(environment_server)
    if selected_environment_server:
        return ProxySettings(
            selected_environment_server, environment_username, environment_password
        )
    return ProxySettings.direct()


def _proxy_selector(value: str | None) -> str | None:
    """Normalize a proxy server selector without modifying credentials."""
    if value is None:
        return None
    return value.strip() or None
