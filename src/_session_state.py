"""Private state records for bounded browser sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.browser import BrowserResource
from src.session_key import SessionKey


class SessionCapacityError(RuntimeError):
    """Raised when no idle session can make room for a new session."""

    def __init__(self) -> None:
        """Describe the bounded overload condition without session details."""
        super().__init__("all retained browser sessions are busy or opening")


class SessionLifecycleTimeoutError(RuntimeError):
    """Raised when a caller's bounded lifecycle wait reaches its deadline."""

    def __init__(self) -> None:
        """Describe the stable lifecycle deadline without resource details."""
        super().__init__("browser session lifecycle timed out")


class SessionProxyMismatchError(ValueError):
    """Raised when a session key does not describe the supplied proxy."""

    def __init__(self) -> None:
        """Reject inconsistent key material without exposing either proxy."""
        super().__init__("session key proxy identity does not match supplied proxy")


@dataclass
class OpenResult:
    """The captured outcome of one manager-owned browser creation."""

    resource: BrowserResource | None = None
    error: BaseException | None = None


@dataclass
class SessionEntry:
    """Mutable lifecycle state for one admitted session key."""

    key: SessionKey
    last_used: float
    touch_order: int
    lease: asyncio.Lock = field(default_factory=asyncio.Lock)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    retired: asyncio.Event = field(default_factory=asyncio.Event)
    claims: int = 1
    invalidated: bool = False
    resource: BrowserResource | None = None
    open_task: asyncio.Task[BrowserResource] | None = None
    creation_task: asyncio.Task[OpenResult] | None = None
    retirement_task: asyncio.Task[BaseException | None] | None = None
    terminal_error: BaseException | None = None


@dataclass
class ClaimDecision:
    """One atomic admission decision emitted after the map lock is released."""

    barrier: SessionEntry | None = None
    eviction: SessionEntry | None = None
    result: SessionEntry | None = None
    event: str | None = None
    capacity_rejected: bool = False
