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
    creation_task: asyncio.Task[OpenResult] | None = None
    retirement_task: asyncio.Task[BaseException | None] | None = None
