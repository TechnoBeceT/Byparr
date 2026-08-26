"""Proxy request settings and safe proxy identities."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


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
        """Return a stable, secret-safe identifier for the selected egress."""
        if self.server is None:
            return "direct"
        material = "\x00".join((self.server, self.username or "", self.password or ""))
        return f"proxy:{sha256(material.encode()).hexdigest()}"

    def as_playwright_proxy(self) -> dict[str, str | None] | None:
        """Return the proxy format accepted by InvisiblePlaywright."""
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
    if header_server:
        return ProxySettings(header_server, header_username, header_password)
    if environment_server:
        return ProxySettings(
            environment_server, environment_username, environment_password
        )
    return ProxySettings.direct()
