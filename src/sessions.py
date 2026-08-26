"""Bounded application-lifetime browser sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from src._session_log import log_session_event
from src._session_state import OpenResult, SessionCapacityError, SessionEntry
from src.browser import BrowserDepClass, BrowserFactory
from src.consts import SESSION_MAX_SESSIONS, SESSION_TTL_SECONDS
from src.proxy import ProxySettings
from src.session_key import SessionKey

_CLOSED_MESSAGE = "session manager is closed"
_TTL_MIN_MESSAGE = "ttl_seconds must be at least 1"
_CAPACITY_MIN_MESSAGE = "max_sessions must be at least 1"


class SessionManager:
    """Retain, serialize, expire, and close a bounded set of browser resources."""

    def __init__(
        self,
        factory: BrowserFactory,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = SESSION_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure session retention limits and the monotonic time source."""
        if ttl_seconds < 1:
            raise ValueError(_TTL_MIN_MESSAGE)
        if max_sessions < 1:
            raise ValueError(_CAPACITY_MIN_MESSAGE)
        self._factory = factory
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._entries: dict[SessionKey, SessionEntry] = {}
        self._map_lock = asyncio.Lock()
        self._touch_order = 0
        self._closed = False
        self._close_task: asyncio.Task[BaseException | None] | None = None

    @asynccontextmanager
    async def acquire(
        self, key: SessionKey, proxy: ProxySettings
    ) -> AsyncIterator[BrowserDepClass]:
        """Lease the retained browser for one key, creating it only once."""
        while True:
            entry = await self._claim_entry(key, proxy)
            lease_acquired = False
            used = False
            retry = False
            try:
                creation_task = entry.creation_task
                assert creation_task is not None
                outcome = await asyncio.shield(creation_task)
                if outcome.error is not None:
                    raise outcome.error
                resource = outcome.resource
                assert resource is not None

                await entry.lease.acquire()
                lease_acquired = True
                async with self._map_lock:
                    closed = self._closed
                    unavailable = (
                        entry.invalidated or self._entries.get(key) is not entry
                    )
                if closed:
                    raise RuntimeError(_CLOSED_MESSAGE)
                if unavailable:
                    retry = True
                else:
                    used = True
                    log_session_event(logging.DEBUG, "leased", key)
                    yield BrowserDepClass(resource.page, resource.context)
                    return
            finally:
                cleanup = asyncio.create_task(
                    self._release_claim(entry, lease_acquired=lease_acquired, used=used)
                )
                await asyncio.shield(cleanup)

            if retry:
                await asyncio.shield(entry.retired.wait())

    async def reset(self, session: str) -> int:
        """Retire every site and proxy entry for one exact normalized session."""
        async with self._map_lock:
            entries = [
                entry
                for entry in self._entries.values()
                if entry.key.session == session and not entry.invalidated
            ]
            tasks = [self._schedule_retirement_locked(entry) for entry in entries]
        error = await self._wait_for_retirements(tasks)
        if error is not None:
            raise error
        return len(entries)

    async def expire_idle(self) -> int:
        """Retire entries idle for at least the configured monotonic TTL."""
        now = self._clock()
        async with self._map_lock:
            entries = [
                entry
                for entry in self._entries.values()
                if not entry.invalidated
                and entry.claims == 0
                and entry.creation_task is not None
                and entry.creation_task.done()
                and now - entry.last_used >= self._ttl_seconds
            ]
            tasks = [
                self._schedule_retirement_locked(entry, remove=True)
                for entry in entries
            ]
        error = await self._wait_for_retirements(tasks)
        if error is not None:
            raise error
        return len(entries)

    async def invalidate(self, key: SessionKey) -> bool:
        """Mark one fatally unusable resource for retirement after its lease drains."""
        async with self._map_lock:
            entry = self._entries.get(key)
            if entry is None or entry.invalidated:
                return False
            self._schedule_retirement_locked(
                entry,
                remove=entry.claims == 0,
            )
        return True

    async def close(self) -> None:
        """Reject new leases and drain every retained or opening resource once."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        error = await asyncio.shield(self._close_task)
        if error is not None:
            raise error

    async def _claim_entry(self, key: SessionKey, proxy: ProxySettings) -> SessionEntry:
        """Reserve one claim, waiting only for same-key retirement when necessary."""
        while True:
            wait_for: asyncio.Event | None = None
            async with self._map_lock:
                if self._closed:
                    raise RuntimeError(_CLOSED_MESSAGE)
                existing = self._entries.get(key)
                if existing is not None:
                    if existing.invalidated:
                        wait_for = existing.retired
                    else:
                        if existing.claims == 0:
                            existing.drained.clear()
                        existing.claims += 1
                        return existing
                else:
                    prerequisite: asyncio.Task[BaseException | None] | None = None
                    if len(self._entries) >= self._max_sessions:
                        candidate = self._lru_idle_entry_locked()
                        if candidate is None:
                            log_session_event(logging.WARNING, "capacity_rejected", key)
                            raise SessionCapacityError
                        prerequisite = self._schedule_retirement_locked(
                            candidate, remove=True
                        )
                    entry = self._new_entry_locked(key, proxy, prerequisite)
                    self._entries[key] = entry
                    log_session_event(logging.DEBUG, "admitted", key)
                    return entry
            assert wait_for is not None
            await asyncio.shield(wait_for.wait())

    def _new_entry_locked(
        self,
        key: SessionKey,
        proxy: ProxySettings,
        prerequisite: asyncio.Task[BaseException | None] | None,
    ) -> SessionEntry:
        """Create an admitted entry whose opening work is manager-owned."""
        entry = SessionEntry(
            key=key,
            last_used=self._clock(),
            touch_order=self._next_touch_order(),
        )
        entry.creation_task = asyncio.create_task(
            self._open_entry(entry, proxy, prerequisite)
        )
        return entry

    async def _open_entry(
        self,
        entry: SessionEntry,
        proxy: ProxySettings,
        prerequisite: asyncio.Task[BaseException | None] | None,
    ) -> OpenResult:
        """Open once and preserve the outcome for every same-key waiter."""
        if prerequisite is not None:
            prerequisite_error = await asyncio.shield(prerequisite)
            if prerequisite_error is not None:
                await self._forget_failed_entry(entry)
                return OpenResult(error=prerequisite_error)
        try:
            resource = await self._factory.open(proxy)
        except BaseException as error:
            await self._forget_failed_entry(entry)
            log_session_event(logging.WARNING, "open_failed", entry.key, error)
            return OpenResult(error=error)
        async with self._map_lock:
            entry.resource = resource
        log_session_event(logging.DEBUG, "opened", entry.key)
        return OpenResult(resource=resource)

    async def _forget_failed_entry(self, entry: SessionEntry) -> None:
        """Remove a failed creation so a later acquisition can retry."""
        async with self._map_lock:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key)
            entry.invalidated = True
            entry.retired.set()

    async def _release_claim(
        self, entry: SessionEntry, *, lease_acquired: bool, used: bool
    ) -> None:
        """Release one waiter or active lease and publish its last-use time."""
        if lease_acquired:
            entry.lease.release()
        async with self._map_lock:
            entry.claims -= 1
            if used:
                entry.last_used = self._clock()
                entry.touch_order = self._next_touch_order()
            if entry.claims == 0:
                entry.drained.set()

    def _lru_idle_entry_locked(self) -> SessionEntry | None:
        """Select the stable least-recently-used entry that is safe to close."""
        candidates = [
            entry
            for entry in self._entries.values()
            if not entry.invalidated
            and entry.claims == 0
            and entry.creation_task is not None
            and entry.creation_task.done()
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda entry: (entry.last_used, entry.touch_order),
        )

    def _schedule_retirement_locked(
        self, entry: SessionEntry, *, remove: bool = False
    ) -> asyncio.Task[BaseException | None]:
        """Make an entry unavailable and start its manager-owned retirement."""
        entry.invalidated = True
        if remove and self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key)
        if entry.retirement_task is None:
            entry.retirement_task = asyncio.create_task(self._finish_retirement(entry))
        return entry.retirement_task

    async def _finish_retirement(self, entry: SessionEntry) -> BaseException | None:
        """Wait for creation and claims, remove admission, then close outside locks."""
        creation_task = entry.creation_task
        assert creation_task is not None
        outcome = await asyncio.shield(creation_task)
        await entry.drained.wait()
        async with self._map_lock:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key)
        error: BaseException | None = None
        if outcome.resource is not None:
            try:
                await outcome.resource.close()
            except BaseException as close_error:
                error = close_error
                log_session_event(
                    logging.WARNING, "close_failed", entry.key, close_error
                )
        entry.retired.set()
        log_session_event(logging.DEBUG, "retired", entry.key)
        return error

    async def _close_owned(self) -> BaseException | None:
        """Own manager shutdown independently of any one caller's cancellation."""
        async with self._map_lock:
            self._closed = True
            entries = list(self._entries.values())
            tasks = [
                self._schedule_retirement_locked(entry, remove=True)
                for entry in entries
            ]
        return await self._wait_for_retirements(tasks)

    @staticmethod
    async def _wait_for_retirements(
        tasks: list[asyncio.Task[BaseException | None]],
    ) -> BaseException | None:
        """Wait for every selected close and return the first cleanup error."""
        if not tasks:
            return None
        errors = await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        return next((error for error in errors if error is not None), None)

    def _next_touch_order(self) -> int:
        self._touch_order += 1
        return self._touch_order
