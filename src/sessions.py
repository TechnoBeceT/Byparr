"""Bounded application-lifetime browser sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from src._session_log import log_session_event
from src._session_state import (
    AdmissionReservation,
    ClaimDecision,
    OpenResult,
    SessionCapacityError,
    SessionEntry,
    SessionLifecycleTimeoutError,
    SessionProxyMismatchError,
)
from src.browser import BrowserDepClass, BrowserFactory
from src.consts import (
    SESSION_LIFECYCLE_TIMEOUT_SECONDS,
    SESSION_MAX_SESSIONS,
    SESSION_TTL_SECONDS,
)
from src.proxy import ProxySettings
from src.session_key import SessionKey

_CLOSED_MESSAGE = "session manager is closed"
_TTL_MIN_MESSAGE = "ttl_seconds must be at least 1"
_CAPACITY_MIN_MESSAGE = "max_sessions must be at least 1"
_LIFECYCLE_TIMEOUT_MIN_MESSAGE = "lifecycle_timeout_seconds must be at least 1"
_lifecycle_timeout = asyncio.timeout


class SessionManager:
    """Retain, serialize, expire, and close a bounded set of browser resources."""

    def __init__(
        self,
        factory: BrowserFactory,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = SESSION_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        lifecycle_timeout_seconds: int = SESSION_LIFECYCLE_TIMEOUT_SECONDS,
    ) -> None:
        """Configure session limits, deadlines, and the monotonic time source."""
        if ttl_seconds < 1:
            raise ValueError(_TTL_MIN_MESSAGE)
        if max_sessions < 1:
            raise ValueError(_CAPACITY_MIN_MESSAGE)
        if lifecycle_timeout_seconds < 1:
            raise ValueError(_LIFECYCLE_TIMEOUT_MIN_MESSAGE)
        self._factory = factory
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._lifecycle_timeout_seconds = lifecycle_timeout_seconds
        self._entries: dict[SessionKey, SessionEntry] = {}
        self._owned: dict[int, SessionEntry] = {}
        self._barriers: dict[SessionKey, SessionEntry] = {}
        self._admissions: dict[SessionKey, AdmissionReservation] = {}
        self._map_lock = asyncio.Lock()
        self._touch_order = 0
        self._closed = False
        self._shutdown_task: asyncio.Task[BaseException | None] | None = None

    @asynccontextmanager
    async def acquire(
        self, key: SessionKey, proxy: ProxySettings
    ) -> AsyncIterator[BrowserDepClass]:
        """Lease the retained browser for one key, creating it only once."""
        if key.proxy_id != proxy.identity:
            raise SessionProxyMismatchError
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
                    yield BrowserDepClass(resource.page, resource.context)
                    return
            finally:
                cleanup = asyncio.create_task(
                    self._release_claim(entry, lease_acquired=lease_acquired, used=used)
                )
                await asyncio.shield(cleanup)

            if retry:
                await self._wait_for_event(entry.retired, key, "replacement")

    async def reset(self, session: str) -> int:
        """Retire every site and proxy entry for one exact normalized session."""
        async with self._map_lock:
            entries = [
                entry for entry in self._owned.values() if entry.key.session == session
            ]
            tasks = [self._schedule_retirement_locked(entry) for entry in entries]
        self._log_entries("reset", entries)
        error = await self._wait_for_retirements(tasks, entries, "reset")
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
                if entry.claims == 0
                and entry.creation_task is not None
                and entry.creation_task.done()
                and now - entry.last_used >= self._ttl_seconds
            ]
            tasks = [self._schedule_retirement_locked(entry) for entry in entries]
        self._log_entries("expire", entries)
        error = await self._wait_for_retirements(tasks, entries, "expire")
        if error is not None:
            raise error
        return len(entries)

    async def invalidate(self, key: SessionKey) -> bool:
        """Mark one fatally unusable resource for retirement after its lease drains."""
        async with self._map_lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            self._schedule_retirement_locked(entry)
        log_session_event(logging.DEBUG, "invalidate", key)
        return True

    async def close(self) -> None:
        """Reject new leases and boundedly await all manager-owned cleanup."""
        task, entries, started = await self._begin_shutdown()
        if started:
            self._log_entries("shutdown", entries)
        key = entries[0].key if entries else None
        error = await self._wait_for_task(task, key, "shutdown")
        if error is not None:
            raise error

    async def _claim_entry(self, key: SessionKey, proxy: ProxySettings) -> SessionEntry:
        """Reserve one claim without losing ownership of retiring resources."""
        while True:
            async with self._map_lock:
                decision = self._decide_claim_locked(key, proxy)
            if decision.event is not None:
                log_session_event(logging.DEBUG, decision.event, key)
            if decision.result is not None:
                return decision.result
            if decision.capacity_rejected:
                log_session_event(logging.WARNING, "capacity", key)
                raise SessionCapacityError
            if decision.barrier is not None:
                await self._wait_for_event(decision.barrier.retired, key, "replacement")
                continue
            reservation = decision.reservation
            assert reservation is not None
            if decision.eviction is not None:
                log_session_event(logging.DEBUG, "evict", decision.eviction.key)
            reservation_task = reservation.task
            assert reservation_task is not None
            error = await self._wait_for_task(reservation_task, key, "evict")
            if error is not None:
                raise error

    def _decide_claim_locked(
        self, key: SessionKey, proxy: ProxySettings
    ) -> ClaimDecision:
        """Mutate admission atomically and return logging/wait work for later."""
        if self._closed:
            raise RuntimeError(_CLOSED_MESSAGE)
        barrier = self._barriers.get(key)
        if barrier is not None:
            return ClaimDecision(barrier=barrier)
        reservation = self._admissions.get(key)
        if reservation is not None:
            return ClaimDecision(reservation=reservation)
        existing = self._entries.get(key)
        if existing is not None:
            if existing.claims == 0:
                existing.drained.clear()
            existing.claims += 1
            return ClaimDecision(result=existing, event="reuse")
        if self._capacity_count_locked() >= self._max_sessions:
            eviction = self._lru_idle_entry_locked()
            if eviction is None:
                return ClaimDecision(capacity_rejected=True)
            self._schedule_retirement_locked(eviction)
            reservation = AdmissionReservation(key=key, victim=eviction)
            self._admissions[key] = reservation
            reservation.task = asyncio.create_task(
                self._finish_admission(reservation, proxy)
            )
            return ClaimDecision(reservation=reservation, eviction=eviction)
        return ClaimDecision(result=self._new_entry_locked(key, proxy), event="create")

    def _new_entry_locked(
        self, key: SessionKey, proxy: ProxySettings, *, claims: int = 1
    ) -> SessionEntry:
        """Create an admitted entry and register ownership before opening."""
        entry = SessionEntry(
            key=key,
            last_used=self._clock(),
            touch_order=self._next_touch_order(),
            claims=claims,
        )
        if claims == 0:
            entry.drained.set()
        self._entries[key] = entry
        self._owned[id(entry)] = entry
        entry.creation_task = asyncio.create_task(self._open_entry(entry, proxy))
        return entry

    async def _finish_admission(
        self, reservation: AdmissionReservation, proxy: ProxySettings
    ) -> BaseException | None:
        """Replace one retired slot exactly once for all same-key callers."""
        retirement_task = reservation.victim.retirement_task
        assert retirement_task is not None
        error = await asyncio.shield(retirement_task)
        created = False
        async with self._map_lock:
            if self._admissions.get(reservation.key) is not reservation:
                return error
            self._admissions.pop(reservation.key)
            if error is None and not self._closed:
                self._new_entry_locked(reservation.key, proxy, claims=0)
                created = True
        if created:
            log_session_event(logging.DEBUG, "create", reservation.key)
        return error

    async def _open_entry(
        self, entry: SessionEntry, proxy: ProxySettings
    ) -> OpenResult:
        """Open once, timing out callers while retaining cleanup ownership."""
        open_task = asyncio.create_task(self._factory.open(proxy))
        entry.open_task = open_task
        try:
            async with _lifecycle_timeout(self._lifecycle_timeout_seconds):
                resource = await asyncio.shield(open_task)
        except TimeoutError:
            open_task.cancel()
            async with self._map_lock:
                self._schedule_retirement_locked(entry)
            error = SessionLifecycleTimeoutError()
            log_session_event(
                logging.WARNING, "timeout", entry.key, error, reason="open"
            )
            return OpenResult(error=error)
        except BaseException as error:
            await self._terminalize_entry(entry, None)
            return OpenResult(error=error)
        async with self._map_lock:
            entry.resource = resource
        return OpenResult(resource=resource)

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
        """Select the stable least-recently-used admitted idle entry."""
        candidates = [
            entry
            for entry in self._entries.values()
            if entry.claims == 0
            and entry.creation_task is not None
            and entry.creation_task.done()
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda entry: (entry.last_used, entry.touch_order))

    def _capacity_count_locked(self) -> int:
        """Count live resources plus replacement slots whose victims are terminal."""
        released_reservations = sum(
            id(reservation.victim) not in self._owned
            for reservation in self._admissions.values()
        )
        return len(self._owned) + released_reservations

    def _schedule_retirement_locked(
        self, entry: SessionEntry
    ) -> asyncio.Task[BaseException | None]:
        """Remove admission, install its key barrier, and own retirement."""
        entry.invalidated = True
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key)
        self._barriers[entry.key] = entry
        if entry.retirement_task is None:
            entry.retirement_task = asyncio.create_task(self._finish_retirement(entry))
        return entry.retirement_task

    async def _finish_retirement(self, entry: SessionEntry) -> BaseException | None:
        """Drain creation and leases, close once, then release ownership/barriers."""
        creation_task = entry.creation_task
        assert creation_task is not None
        outcome = await asyncio.shield(creation_task)
        await entry.drained.wait()
        error: BaseException | None = None
        if outcome.resource is not None:
            error = await self._close_resource(outcome.resource)
        elif isinstance(outcome.error, SessionLifecycleTimeoutError):
            error = await self._finish_timed_out_open(entry)
        await self._terminalize_entry(entry, error)
        return error

    async def _finish_timed_out_open(self, entry: SessionEntry) -> BaseException | None:
        """Consume a cancelled open and close a resource if cancellation lost."""
        open_task = entry.open_task
        assert open_task is not None
        try:
            resource = await open_task
        except BaseException:
            return None
        return await self._close_resource(resource)

    @staticmethod
    async def _close_resource(resource) -> BaseException | None:
        """Close to terminal state while retaining cleanup errors as results."""
        try:
            await resource.close()
        except BaseException as error:
            return error
        return None

    async def _terminalize_entry(
        self, entry: SessionEntry, error: BaseException | None
    ) -> None:
        """Release ownership only after opening or cleanup is truly terminal."""
        async with self._map_lock:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key)
            if self._barriers.get(entry.key) is entry:
                self._barriers.pop(entry.key)
            self._owned.pop(id(entry), None)
            entry.terminal_error = error
            entry.retired.set()

    async def _begin_shutdown(
        self,
    ) -> tuple[
        asyncio.Task[BaseException | None],
        list[SessionEntry],
        bool,
    ]:
        """Atomically reject acquisition and start one unbounded owned drain."""
        async with self._map_lock:
            if self._shutdown_task is not None:
                return self._shutdown_task, list(self._owned.values()), False
            self._closed = True
            entries = list(self._owned.values())
            tasks = [self._schedule_retirement_locked(entry) for entry in entries]
            tasks.extend(
                reservation.task
                for reservation in self._admissions.values()
                if reservation.task is not None
            )
            self._shutdown_task = asyncio.create_task(self._finish_shutdown(tasks))
            return self._shutdown_task, entries, True

    @staticmethod
    async def _finish_shutdown(
        tasks: list[asyncio.Task[BaseException | None]],
    ) -> BaseException | None:
        """Await every owned retirement and return the first cleanup error."""
        if not tasks:
            return None
        errors = await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        return next((error for error in errors if error is not None), None)

    async def _wait_for_retirements(
        self,
        tasks: list[asyncio.Task[BaseException | None]],
        entries: list[SessionEntry],
        reason: str,
    ) -> BaseException | None:
        """Bound a caller's aggregate wait without cancelling owned retirement."""
        if not tasks:
            return None
        waiter = asyncio.create_task(self._finish_shutdown(tasks))
        key = entries[0].key if entries else None
        try:
            return await self._wait_for_task(waiter, key, reason)
        finally:
            await self._cancel_ephemeral_task(waiter)

    async def _wait_for_task(
        self,
        task: asyncio.Task[BaseException | None],
        key: SessionKey | None,
        reason: str,
    ) -> BaseException | None:
        """Apply the configured deadline while shielding manager-owned work."""
        try:
            async with _lifecycle_timeout(self._lifecycle_timeout_seconds):
                return await asyncio.shield(task)
        except TimeoutError as timeout:
            error = SessionLifecycleTimeoutError()
            if key is not None:
                log_session_event(logging.WARNING, "timeout", key, error, reason=reason)
            raise error from timeout

    async def _wait_for_event(
        self, event: asyncio.Event, key: SessionKey, reason: str
    ) -> None:
        """Bound a per-key barrier wait without cancelling the shared event."""
        waiter = asyncio.create_task(event.wait())
        try:
            async with _lifecycle_timeout(self._lifecycle_timeout_seconds):
                await asyncio.shield(waiter)
        except TimeoutError as timeout:
            error = SessionLifecycleTimeoutError()
            log_session_event(logging.WARNING, "timeout", key, error, reason=reason)
            raise error from timeout
        finally:
            await self._cancel_ephemeral_task(waiter)

    @staticmethod
    async def _cancel_ephemeral_task(task: asyncio.Task) -> None:
        """Cancel and consume a helper without touching shielded owned work."""
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _log_entries(event: str, entries: list[SessionEntry]) -> None:
        """Emit staged ownership reasons only after releasing the map lock."""
        for entry in entries:
            log_session_event(logging.DEBUG, event, entry.key)

    def _next_touch_order(self) -> int:
        self._touch_order += 1
        return self._touch_order
