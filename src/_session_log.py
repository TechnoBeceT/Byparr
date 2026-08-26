"""Credential-safe structured logging for browser-session lifecycle events."""

from __future__ import annotations

import hmac
import logging
import secrets
from hashlib import sha256

from src._session_state import SessionPoolSnapshot
from src.session_key import SessionKey

logger = logging.getLogger("uvicorn.error")
_LOG_DIGEST_KEY = secrets.token_bytes(32)


def log_session_event(
    level: int,
    event: str,
    key: SessionKey,
    error: BaseException | None = None,
    *,
    reason: str | None = None,
    snapshot: SessionPoolSnapshot | None = None,
) -> None:
    """Log lifecycle events with keyed digests instead of identifiers."""
    fields: dict[str, object] = {
        "event": event,
        "session_digest": _digest(key.session),
        "site_digest": _digest(key.site),
        "proxy_digest": _digest(key.proxy_id),
    }
    if error is not None:
        fields["error_type"] = type(error).__name__
    if reason is not None:
        fields["reason"] = reason
    if snapshot is not None:
        fields.update(
            {
                "active_count": snapshot.active_count,
                "idle_count": snapshot.idle_count,
                "busy_count": snapshot.busy_count,
                "opening_count": snapshot.opening_count,
                "retiring_count": snapshot.retiring_count,
                "admission_count": snapshot.admission_count,
                "capacity_count": snapshot.capacity_count,
                "capacity_limit": snapshot.capacity_limit,
            }
        )
    rendered = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.log(
        max(level, logging.INFO),
        f"browser_session_lifecycle {rendered}",
        extra=fields,
    )


def _digest(value: str) -> str:
    """Return a process-private short digest suitable only for correlation."""
    return hmac.new(_LOG_DIGEST_KEY, value.encode(), sha256).hexdigest()[:16]
