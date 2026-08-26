"""Credential-safe structured logging for browser-session lifecycle events."""

from __future__ import annotations

import hmac
import logging
import secrets
from hashlib import sha256

from src.session_key import SessionKey

logger = logging.getLogger("src.sessions")
_LOG_DIGEST_KEY = secrets.token_bytes(32)


def log_session_event(
    level: int,
    event: str,
    key: SessionKey,
    error: BaseException | None = None,
    reason: str | None = None,
) -> None:
    """Log lifecycle events with keyed digests instead of identifiers."""
    fields: dict[str, str] = {
        "event": event,
        "session_digest": _digest(key.session),
        "site_digest": _digest(key.site),
        "proxy_digest": _digest(key.proxy_id),
    }
    if error is not None:
        fields["error_type"] = type(error).__name__
    if reason is not None:
        fields["reason"] = reason
    logger.log(level, "browser_session_lifecycle", extra=fields)


def _digest(value: str) -> str:
    """Return a process-private short digest suitable only for correlation."""
    return hmac.new(_LOG_DIGEST_KEY, value.encode(), sha256).hexdigest()[:16]
