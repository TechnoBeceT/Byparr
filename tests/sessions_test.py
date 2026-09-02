# ruff: noqa: D102, D103, D105, D107, PLR2004, S105

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from io import StringIO
from types import TracebackType
from typing import cast

import pytest
from pydantic import ValidationError

from src import sessions
from src.consts import Settings
from src.proxy import ProxySettings
from src.session_key import SessionKey
from src.sessions import SessionCapacityError, SessionManager
from src.utils import logger as production_logger


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeResource:
    def __init__(
        self,
        name: str,
        *,
        close_error: BaseException | None = None,
        close_started: asyncio.Event | None = None,
        allow_close: asyncio.Event | None = None,
        tracker: LiveResourceTracker | None = None,
    ) -> None:
        self.name = name
        self.page = f"page:{name}"
        self.context = f"context:{name}"
        self.solver = f"solver:{name}"
        self.close_error = close_error
        self.close_calls = 0
        self.closed = asyncio.Event()
        self.close_started = close_started
        self.allow_close = allow_close
        self.tracker = tracker

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.allow_close is not None:
            await self.allow_close.wait()
        if self.tracker is not None:
            self.tracker.closed()
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


class FakeFactory:
    def __init__(self, outcomes: Iterable[FakeResource | BaseException] = ()) -> None:
        self.outcomes: deque[FakeResource | BaseException] = deque(outcomes)
        self.calls: list[ProxySettings] = []
        self.open_started = asyncio.Event()
        self.allow_open: asyncio.Event | None = None
        self.resources: list[FakeResource] = []

    async def open(self, proxy: ProxySettings) -> FakeResource:
        self.calls.append(proxy)
        self.open_started.set()
        if self.allow_open is not None:
            await self.allow_open.wait()
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            resource = outcome
        else:
            resource = FakeResource(str(len(self.resources) + 1))
        self.resources.append(resource)
        return resource


class LiveResourceTracker:
    def __init__(self) -> None:
        self.live = 0
        self.maximum = 0

    def opened(self) -> None:
        self.live += 1
        self.maximum = max(self.maximum, self.live)

    def closed(self) -> None:
        self.live -= 1


class TrackedFactory:
    def __init__(self, tracker: LiveResourceTracker) -> None:
        self.tracker = tracker
        self.calls: list[ProxySettings] = []
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.resources: list[FakeResource] = []

    async def open(self, proxy: ProxySettings) -> FakeResource:
        self.calls.append(proxy)
        self.tracker.opened()
        resource = FakeResource(
            str(len(self.resources) + 1),
            close_started=self.close_started if not self.resources else None,
            allow_close=self.allow_close if not self.resources else None,
            tracker=self.tracker,
        )
        self.resources.append(resource)
        return resource


class CapacityFactory:
    def __init__(self, blocked_resources: int) -> None:
        self.tracker = LiveResourceTracker()
        self.calls: list[ProxySettings] = []
        self.allow_close = asyncio.Event()
        self.close_started = [asyncio.Event() for _ in range(blocked_resources)]
        self.resources: list[FakeResource] = []

    async def open(self, proxy: ProxySettings) -> FakeResource:
        self.calls.append(proxy)
        self.tracker.opened()
        index = len(self.resources)
        resource = FakeResource(
            str(index + 1),
            close_started=(
                self.close_started[index] if index < len(self.close_started) else None
            ),
            allow_close=(self.allow_close if index < len(self.close_started) else None),
            tracker=self.tracker,
        )
        self.resources.append(resource)
        return resource


class CancellingOpenFactory:
    def __init__(self) -> None:
        self.calls: list[ProxySettings] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def open(self, proxy: ProxySettings) -> FakeResource:
        self.calls.append(proxy)
        self.started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.allow_cleanup.wait()
            self.cleanup_finished.set()
            raise


class ImmediateTimeout:
    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None
        self.cancellation: asyncio.Handle | None = None

    async def __aenter__(self) -> None:
        self.task = asyncio.current_task()
        assert self.task is not None
        self.cancellation = asyncio.get_running_loop().call_soon(self.task.cancel)

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if self.cancellation is not None:
            self.cancellation.cancel()
        if error_type is asyncio.CancelledError:
            assert self.task is not None
            self.task.uncancel()
            raise TimeoutError
        return False


def immediate_timeout(_seconds: float) -> ImmediateTimeout:
    return ImmediateTimeout()


async def next_turn() -> None:
    """Yield through an event-loop callback without timing-based sleeps."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


def pending_coroutines(name: str) -> list[asyncio.Task[object]]:
    current = asyncio.current_task()
    return [
        cast("asyncio.Task[object]", task)
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and name in getattr(task.get_coro(), "__qualname__", "")
    ]


def key(session: str, site: str, proxy: ProxySettings | None = None) -> SessionKey:
    selected = proxy or ProxySettings.direct()
    return SessionKey(session=session, site=site, proxy_id=selected.identity)


def test_session_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(session_ttl_seconds=0)
    with pytest.raises(ValidationError):
        Settings(session_max_sessions=0)
    with pytest.raises(ValidationError):
        Settings(session_lifecycle_timeout_seconds=0)


def test_session_settings_have_bounded_defaults() -> None:
    settings = Settings()

    assert settings.session_ttl_seconds == 900
    assert settings.session_max_sessions == 8
    assert settings.session_lifecycle_timeout_seconds == 120


@pytest.mark.asyncio
async def test_same_key_reuses_one_retained_resource() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()) as first:
        pass
    async with manager.acquire(session_key, ProxySettings.direct()) as second:
        pass

    assert first == second
    assert len(factory.calls) == 1
    assert factory.resources[0].close_calls == 0
    await manager.close()


@pytest.mark.asyncio
async def test_same_key_leases_are_serialized() -> None:
    manager = SessionManager(FakeFactory())
    session_key = key("account-a", "example.com")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()
    order: list[str] = []

    async def first_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            order.append("first-entered")
            first_entered.set()
            await release_first.wait()
            order.append("first-releasing")

    async def second_lease() -> None:
        second_attempted.set()
        async with manager.acquire(session_key, ProxySettings.direct()):
            order.append("second-entered")
            second_entered.set()

    first = asyncio.create_task(first_lease())
    await first_entered.wait()
    second = asyncio.create_task(second_lease())
    await second_attempted.wait()
    await next_turn()

    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first, second)

    assert order == ["first-entered", "first-releasing", "second-entered"]
    await manager.close()


@pytest.mark.asyncio
async def test_different_keys_lease_in_parallel() -> None:
    manager = SessionManager(FakeFactory())
    release = asyncio.Event()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()

    async def lease(session_key: SessionKey, entered: asyncio.Event) -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            entered.set()
            await release.wait()

    first = asyncio.create_task(lease(key("a", "one.example"), first_entered))
    second = asyncio.create_task(lease(key("b", "two.example"), second_entered))

    await asyncio.gather(first_entered.wait(), second_entered.wait())
    release.set()
    await asyncio.gather(first, second)
    await manager.close()


@pytest.mark.asyncio
async def test_concurrent_first_acquisition_creates_once() -> None:
    factory = FakeFactory()
    factory.allow_open = asyncio.Event()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")
    acquired: list[object] = []

    async def lease_once() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()) as browser:
            acquired.append(browser)

    first = asyncio.create_task(lease_once())
    await factory.open_started.wait()
    second = asyncio.create_task(lease_once())
    await next_turn()

    assert len(factory.calls) == 1

    factory.allow_open.set()
    await asyncio.gather(first, second)

    assert acquired[0] == acquired[1]
    assert len(factory.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_open_failure_allows_a_later_retry() -> None:
    replacement = FakeResource("replacement")
    factory = FakeFactory([RuntimeError("open failed"), replacement])
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")

    with pytest.raises(RuntimeError, match="open failed"):
        async with manager.acquire(session_key, ProxySettings.direct()):
            pass

    async with manager.acquire(session_key, ProxySettings.direct()) as browser:
        assert browser.page == "page:replacement"

    assert len(factory.calls) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_opener_does_not_cancel_shared_creation() -> None:
    factory = FakeFactory()
    factory.allow_open = asyncio.Event()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")

    cancelled = asyncio.create_task(
        manager.acquire(session_key, ProxySettings.direct()).__aenter__()
    )
    await factory.open_started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    acquired = asyncio.Event()

    async def surviving_waiter() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            acquired.set()

    survivor = asyncio.create_task(surviving_waiter())
    factory.allow_open.set()
    await acquired.wait()
    await survivor

    assert len(factory.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_expire_idle_uses_monotonic_last_use() -> None:
    clock = FakeClock()
    factory = FakeFactory()
    manager = SessionManager(factory, ttl_seconds=900, clock=clock)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        pass

    clock.value = 899
    assert await manager.expire_idle() == 0
    clock.value = 900
    assert await manager.expire_idle() == 1
    assert factory.resources[0].close_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_expiry_never_closes_a_busy_entry() -> None:
    clock = FakeClock()
    factory = FakeFactory()
    manager = SessionManager(factory, ttl_seconds=10, clock=clock)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        clock.value = 100
        assert await manager.expire_idle() == 0
        assert factory.resources[0].close_calls == 0

    clock.value = 110
    assert await manager.expire_idle() == 1
    assert factory.resources[0].close_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_capacity_evicts_the_deterministic_lru_idle_entry() -> None:
    clock = FakeClock()
    factory = FakeFactory()
    manager = SessionManager(factory, max_sessions=2, clock=clock)
    first_key = key("a", "one.example")
    second_key = key("b", "two.example")
    third_key = key("c", "three.example")

    async with manager.acquire(first_key, ProxySettings.direct()):
        pass
    clock.value = 1
    async with manager.acquire(second_key, ProxySettings.direct()):
        pass
    clock.value = 2
    async with manager.acquire(first_key, ProxySettings.direct()):
        pass
    clock.value = 3
    async with manager.acquire(third_key, ProxySettings.direct()):
        pass

    assert len(factory.calls) == 3
    assert factory.resources[0].close_calls == 0
    assert factory.resources[1].close_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_capacity_fails_fast_when_every_entry_is_busy() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory, max_sessions=1)

    async with manager.acquire(key("a", "one.example"), ProxySettings.direct()):
        with pytest.raises(SessionCapacityError):
            async with manager.acquire(key("b", "two.example"), ProxySettings.direct()):
                pass

    assert len(factory.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_same_key_capacity_replacement_is_shared_at_limit_one() -> None:
    factory = CapacityFactory(blocked_resources=1)
    manager = SessionManager(factory, max_sessions=1)
    destination = key("new", "new.example")

    async with manager.acquire(key("old", "old.example"), ProxySettings.direct()):
        pass

    async def lease_destination() -> None:
        async with manager.acquire(destination, ProxySettings.direct()):
            pass

    first = asyncio.create_task(lease_destination())
    await factory.close_started[0].wait()
    second = asyncio.create_task(lease_destination())
    await next_turn()

    factory.allow_close.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert results == [None, None]
    assert len(factory.calls) == 2
    assert factory.tracker.maximum == 1
    await manager.close()


@pytest.mark.asyncio
async def test_same_key_capacity_replacement_preserves_second_idle_entry() -> None:
    factory = CapacityFactory(blocked_resources=2)
    manager = SessionManager(factory, max_sessions=2)
    destination = key("new", "new.example")

    async with manager.acquire(key("old-a", "a.example"), ProxySettings.direct()):
        pass
    async with manager.acquire(key("old-b", "b.example"), ProxySettings.direct()):
        pass

    async def lease_destination() -> None:
        async with manager.acquire(destination, ProxySettings.direct()):
            pass

    first = asyncio.create_task(lease_destination())
    await factory.close_started[0].wait()
    second = asyncio.create_task(lease_destination())
    await next_turn()
    second_idle_was_evicted = factory.close_started[1].is_set()

    factory.allow_close.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert results == [None, None]
    assert not second_idle_was_evicted
    assert sum(resource.close_calls for resource in factory.resources[:2]) == 1
    assert len(factory.calls) == 3
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_capacity_replacer_does_not_cancel_shared_admission() -> None:
    factory = CapacityFactory(blocked_resources=1)
    manager = SessionManager(factory, max_sessions=1)
    destination = key("new", "new.example")

    async with manager.acquire(key("old", "old.example"), ProxySettings.direct()):
        pass

    async def lease_destination() -> None:
        async with manager.acquire(destination, ProxySettings.direct()):
            pass

    cancelled = asyncio.create_task(lease_destination())
    await factory.close_started[0].wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    survivor = asyncio.create_task(lease_destination())
    await next_turn()
    assert len(factory.calls) == 1

    factory.allow_close.set()
    await survivor

    assert len(factory.calls) == 2
    assert factory.tracker.maximum == 1
    await manager.close()


@pytest.mark.asyncio
async def test_close_waits_for_and_suppresses_pending_admission() -> None:
    factory = CapacityFactory(blocked_resources=1)
    manager = SessionManager(factory, max_sessions=1)

    async with manager.acquire(key("old", "old.example"), ProxySettings.direct()):
        pass

    async def lease_destination() -> None:
        async with manager.acquire(key("new", "new.example"), ProxySettings.direct()):
            pass

    acquiring = asyncio.create_task(lease_destination())
    await factory.close_started[0].wait()
    closing = asyncio.create_task(manager.close())
    await next_turn()
    assert not closing.done()

    factory.allow_close.set()
    results = await asyncio.gather(acquiring, closing, return_exceptions=True)

    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert len(factory.calls) == 1
    assert factory.tracker.live == 0


@pytest.mark.asyncio
async def test_reset_fences_a_matching_pending_admission() -> None:
    factory = CapacityFactory(blocked_resources=1)
    manager = SessionManager(factory, max_sessions=1)
    destination = key("account-a", "new.example")

    async with manager.acquire(key("old", "old.example"), ProxySettings.direct()):
        pass

    async def lease_destination() -> None:
        async with manager.acquire(destination, ProxySettings.direct()):
            pass

    acquiring = asyncio.create_task(lease_destination())
    await factory.close_started[0].wait()
    resetting = asyncio.create_task(manager.reset("account-a"))
    await next_turn()

    assert not resetting.done()

    factory.allow_close.set()
    results = await asyncio.gather(acquiring, resetting, return_exceptions=True)

    assert isinstance(results[0], RuntimeError)
    assert "reset" in str(results[0])
    assert results[1] == 1
    assert len(factory.calls) == 1
    assert factory.tracker.live == 0
    await manager.close()
    assert vars(manager)["_session_generations"] == {}
    assert vars(manager)["_session_acquisitions"] == {}


@pytest.mark.asyncio
async def test_absent_session_resets_do_not_accumulate_generation_state() -> None:
    """Idempotent destroys of unique absent names cannot grow process memory."""
    manager = SessionManager(FakeFactory())

    for number in range(2048):
        assert await manager.reset(f"absent-{number}") == 0

    generations = cast("dict[str, int]", vars(manager)["_session_generations"])
    assert generations == {}
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_poison_the_session_lease() -> None:
    manager = SessionManager(FakeFactory())
    session_key = key("account-a", "example.com")
    waiter_attempted = asyncio.Event()

    async with manager.acquire(session_key, ProxySettings.direct()):

        async def wait_for_lease() -> None:
            waiter_attempted.set()
            async with manager.acquire(session_key, ProxySettings.direct()):
                pass

        waiter = asyncio.create_task(wait_for_lease())
        await waiter_attempted.wait()
        await next_turn()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with manager.acquire(session_key, ProxySettings.direct()):
        pass
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_active_caller_releases_the_session_lease() -> None:
    manager = SessionManager(FakeFactory())
    session_key = key("account-a", "example.com")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def cancelled_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            entered.set()
            await never.wait()

    caller = asyncio.create_task(cancelled_lease())
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    async with manager.acquire(session_key, ProxySettings.direct()):
        pass
    await manager.close()


@pytest.mark.asyncio
async def test_reset_closes_every_site_and_proxy_for_the_exact_session() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory)
    proxy = ProxySettings("http://proxy.test:8080", "user", "password")

    async with manager.acquire(key("account-a", "one.example"), ProxySettings.direct()):
        pass
    async with manager.acquire(key("account-a", "two.example", proxy), proxy):
        pass
    async with manager.acquire(key("account-b", "one.example"), ProxySettings.direct()):
        pass

    assert await manager.reset("account-a") == 2
    assert [resource.close_calls for resource in factory.resources] == [1, 1, 0]

    async with manager.acquire(key("account-b", "one.example"), ProxySettings.direct()):
        pass
    assert len(factory.calls) == 3
    await manager.close()


@pytest.mark.asyncio
async def test_invalidate_retires_a_fatal_active_resource() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        assert await manager.invalidate(session_key)
        assert factory.resources[0].close_calls == 0

    await factory.resources[0].closed.wait()
    async with manager.acquire(session_key, ProxySettings.direct()):
        pass

    assert len(factory.calls) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_invalidate_blocks_same_key_replacement_until_cleanup_finishes() -> None:
    tracker = LiveResourceTracker()
    factory = TrackedFactory(tracker)
    manager = SessionManager(factory, max_sessions=1)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        assert await manager.invalidate(session_key)

    await factory.close_started.wait()
    replacement_entered = asyncio.Event()

    async def replacement_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            replacement_entered.set()

    replacement = asyncio.create_task(replacement_lease())
    await next_turn()
    calls_before_cleanup = len(factory.calls)
    entered_before_cleanup = replacement_entered.is_set()

    factory.allow_close.set()
    await replacement

    assert calls_before_cleanup == 1
    assert not entered_before_cleanup
    assert tracker.maximum == 1
    await manager.close()


@pytest.mark.asyncio
async def test_expiry_blocks_same_key_replacement_until_cleanup_finishes() -> None:
    tracker = LiveResourceTracker()
    factory = TrackedFactory(tracker)
    clock = FakeClock()
    manager = SessionManager(factory, ttl_seconds=1, max_sessions=1, clock=clock)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await factory.close_started.wait()
    replacement_entered = asyncio.Event()

    async def replacement_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            replacement_entered.set()

    replacement = asyncio.create_task(replacement_lease())
    await next_turn()
    calls_before_cleanup = len(factory.calls)
    entered_before_cleanup = replacement_entered.is_set()

    factory.allow_close.set()
    assert await expiring == 1
    await replacement

    assert calls_before_cleanup == 1
    assert not entered_before_cleanup
    assert tracker.maximum == 1
    await manager.close()


@pytest.mark.asyncio
async def test_capacity_counts_a_retiring_resource_until_cleanup_finishes() -> None:
    tracker = LiveResourceTracker()
    factory = TrackedFactory(tracker)
    clock = FakeClock()
    manager = SessionManager(factory, ttl_seconds=1, max_sessions=1, clock=clock)

    async with manager.acquire(key("a", "one.example"), ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await factory.close_started.wait()

    with pytest.raises(SessionCapacityError):
        async with manager.acquire(key("b", "two.example"), ProxySettings.direct()):
            pass

    factory.allow_close.set()
    assert await expiring == 1
    assert len(factory.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_close_waits_for_retirement_already_removed_from_admission() -> None:
    tracker = LiveResourceTracker()
    factory = TrackedFactory(tracker)
    clock = FakeClock()
    manager = SessionManager(factory, ttl_seconds=1, clock=clock)

    async with manager.acquire(key("a", "one.example"), ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await factory.close_started.wait()
    close_started = asyncio.Event()
    close_returned = asyncio.Event()

    async def close_manager() -> None:
        close_started.set()
        await manager.close()
        close_returned.set()

    closing = asyncio.create_task(close_manager())
    await close_started.wait()
    await next_turn()
    closed_before_cleanup = close_returned.is_set()

    factory.allow_close.set()
    await asyncio.gather(expiring, closing)

    assert not closed_before_cleanup
    assert tracker.live == 0


@pytest.mark.asyncio
async def test_close_observes_cleanup_error_from_existing_retirement() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "failing",
        close_error=RuntimeError("cleanup failed"),
        close_started=close_started,
        allow_close=allow_close,
    )
    clock = FakeClock()
    manager = SessionManager(FakeFactory([resource]), ttl_seconds=1, clock=clock)

    async with manager.acquire(key("a", "one.example"), ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await close_started.wait()
    closing = asyncio.create_task(manager.close())
    await next_turn()
    allow_close.set()
    results = await asyncio.gather(expiring, closing, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert all("cleanup failed" in str(result) for result in results)


@pytest.mark.asyncio
async def test_reset_waits_for_matching_entry_already_retiring() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    clock = FakeClock()
    manager = SessionManager(FakeFactory([resource]), ttl_seconds=1, clock=clock)

    async with manager.acquire(key("account-a", "one.example"), ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await close_started.wait()
    reset_returned = asyncio.Event()

    async def reset_session() -> int:
        result = await manager.reset("account-a")
        reset_returned.set()
        return result

    resetting = asyncio.create_task(reset_session())
    await next_turn()
    returned_before_cleanup = reset_returned.is_set()

    allow_close.set()
    assert await expiring == 1
    assert await resetting == 1
    assert not returned_before_cleanup
    await manager.close()


@pytest.mark.asyncio
async def test_open_timeout_cancels_open_and_keeps_cleanup_owned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = CancellingOpenFactory()
    manager = SessionManager(factory, max_sessions=1, lifecycle_timeout_seconds=120)
    session_key = key("account-a", "example.com")

    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        monkeypatch.context() as patch,
    ):
        patch.setattr(sessions, "_lifecycle_timeout", immediate_timeout)
        with pytest.raises(sessions.SessionLifecycleTimeoutError):
            async with manager.acquire(session_key, ProxySettings.direct()):
                pass

    await factory.cancelled.wait()
    with pytest.raises(SessionCapacityError):
        async with manager.acquire(key("b", "two.example"), ProxySettings.direct()):
            pass

    closing = asyncio.create_task(manager.close())
    await next_turn()
    assert not closing.done()

    factory.allow_cleanup.set()
    await factory.cleanup_finished.wait()
    await closing
    assert any(
        getattr(record, "event", None) == "timeout"
        and getattr(record, "reason", None) == "open"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cleanup_timeout_returns_stable_error_while_retirement_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    manager = SessionManager(FakeFactory([resource]), lifecycle_timeout_seconds=120)
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_lifecycle_timeout", immediate_timeout)
        with pytest.raises(
            sessions.SessionLifecycleTimeoutError,
            match="browser session lifecycle timed out",
        ):
            await manager.reset("account-a")

    await close_started.wait()
    replacement_entered = asyncio.Event()

    async def replacement_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            replacement_entered.set()

    replacement = asyncio.create_task(replacement_lease())
    await next_turn()
    assert not replacement_entered.is_set()

    allow_close.set()
    await replacement
    await manager.close()


@pytest.mark.asyncio
async def test_barrier_timeout_and_cancellation_do_not_accumulate_event_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    manager = SessionManager(FakeFactory([resource]))
    session_key = key("account-a", "example.com")

    async with manager.acquire(session_key, ProxySettings.direct()):
        assert await manager.invalidate(session_key)
    await close_started.wait()
    baseline = len(pending_coroutines("Event.wait"))

    async def wait_for_replacement() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_lifecycle_timeout", immediate_timeout)
        for _ in range(2):
            with pytest.raises(sessions.SessionLifecycleTimeoutError):
                await wait_for_replacement()

    for _ in range(2):
        waiter = asyncio.create_task(wait_for_replacement())
        await next_turn()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    await next_turn()
    pending_after_waits = len(pending_coroutines("Event.wait"))
    allow_close.set()
    await manager.close()

    assert pending_after_waits == baseline
    assert resource.close_calls == 1


@pytest.mark.asyncio
async def test_reset_timeouts_do_not_accumulate_aggregate_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    manager = SessionManager(FakeFactory([resource]))

    async with manager.acquire(key("account-a", "example.com"), ProxySettings.direct()):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_lifecycle_timeout", immediate_timeout)
        for _ in range(2):
            with pytest.raises(sessions.SessionLifecycleTimeoutError):
                await manager.reset("account-a")

    await close_started.wait()
    await next_turn()
    pending_waiters = pending_coroutines("SessionManager._finish_shutdown")
    allow_close.set()
    await manager.close()

    assert pending_waiters == []
    assert resource.close_calls == 1


@pytest.mark.asyncio
async def test_cancelled_expiry_cleans_aggregate_waiter() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    clock = FakeClock()
    manager = SessionManager(FakeFactory([resource]), ttl_seconds=1, clock=clock)

    async with manager.acquire(key("account-a", "example.com"), ProxySettings.direct()):
        pass
    clock.value = 1
    expiring = asyncio.create_task(manager.expire_idle())
    await close_started.wait()
    expiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await expiring

    await next_turn()
    pending_waiters = pending_coroutines("SessionManager._finish_shutdown")
    allow_close.set()
    await manager.close()

    assert pending_waiters == []
    assert resource.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_is_retryable_without_cancelling_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource(
        "blocked", close_started=close_started, allow_close=allow_close
    )
    manager = SessionManager(FakeFactory([resource]), lifecycle_timeout_seconds=120)

    async with manager.acquire(key("account-a", "example.com"), ProxySettings.direct()):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_lifecycle_timeout", immediate_timeout)
        with pytest.raises(sessions.SessionLifecycleTimeoutError):
            await manager.close()

    await close_started.wait()
    allow_close.set()
    await manager.close()
    assert resource.close_calls == 1


@pytest.mark.asyncio
async def test_key_proxy_identity_mismatch_fails_closed() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory)
    configured = ProxySettings("http://one.proxy:8080", "one", "password-one")
    supplied = ProxySettings("http://two.proxy:8080", "two", "password-two")

    with pytest.raises(ValueError, match="proxy identity"):
        async with manager.acquire(key("a", "example.com", configured), supplied):
            pass

    assert factory.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_lifecycle_logging_never_runs_under_the_admission_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(FakeFactory())
    session_key = key("account-a", "example.com")
    lock_states: list[bool] = []

    def capture_log(*_args: object, **_kwargs: object) -> None:
        lock = cast("asyncio.Lock", vars(manager)["_map_lock"])
        lock_states.append(lock.locked())

    monkeypatch.setattr(sessions, "log_session_event", capture_log)
    async with manager.acquire(session_key, ProxySettings.direct()):
        pass
    async with manager.acquire(session_key, ProxySettings.direct()):
        pass
    assert await manager.reset("account-a") == 1
    await manager.close()

    assert lock_states
    assert not any(lock_states)


@pytest.mark.asyncio
async def test_lifecycle_logs_distinguish_every_ownership_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    manager = SessionManager(FakeFactory(), ttl_seconds=1, max_sessions=1, clock=clock)
    events: list[str] = []

    def capture_log(_level: int, event: str, *_args: object, **_kwargs: object) -> None:
        events.append(event)

    monkeypatch.setattr(sessions, "log_session_event", capture_log)
    first = key("a", "one.example")
    async with manager.acquire(first, ProxySettings.direct()):
        pass
    async with manager.acquire(first, ProxySettings.direct()):
        pass
    async with manager.acquire(key("b", "two.example"), ProxySettings.direct()):
        pass
    clock.value = 1
    assert await manager.expire_idle() == 1
    async with manager.acquire(key("c", "three.example"), ProxySettings.direct()):
        pass
    assert await manager.reset("c") == 1
    fourth = key("d", "four.example")
    async with manager.acquire(fourth, ProxySettings.direct()):
        assert await manager.invalidate(fourth)
    async with manager.acquire(key("e", "five.example"), ProxySettings.direct()):
        pass
    await manager.close()

    assert {
        "create",
        "reuse",
        "expire",
        "reset",
        "evict",
        "invalidate",
        "shutdown",
    }.issubset(events)


@pytest.mark.asyncio
async def test_close_rejects_new_acquisition_and_drains_active_resources() -> None:
    factory = FakeFactory()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def active_lease() -> None:
        async with manager.acquire(session_key, ProxySettings.direct()):
            entered.set()
            await release.wait()

    lease = asyncio.create_task(active_lease())
    await entered.wait()
    closing = asyncio.create_task(manager.close())
    await next_turn()

    assert factory.resources[0].close_calls == 0
    with pytest.raises(RuntimeError, match="closed"):
        async with manager.acquire(key("b", "two.example"), ProxySettings.direct()):
            pass

    release.set()
    await asyncio.gather(lease, closing)
    assert factory.resources[0].close_calls == 1

    await manager.close()
    assert factory.resources[0].close_calls == 1


@pytest.mark.asyncio
async def test_close_owns_in_flight_creation_after_caller_cancellation() -> None:
    factory = FakeFactory()
    factory.allow_open = asyncio.Event()
    manager = SessionManager(factory)
    session_key = key("account-a", "example.com")

    opening = asyncio.create_task(
        manager.acquire(session_key, ProxySettings.direct()).__aenter__()
    )
    await factory.open_started.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    closing = asyncio.create_task(manager.close())
    await next_turn()
    factory.allow_open.set()
    await closing

    assert factory.resources[0].close_calls == 1


@pytest.mark.asyncio
async def test_lifecycle_logs_contain_only_sanitized_digests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_secret = "session-super-secret"
    site_secret = "customer-secret.example"
    proxy_secret = "proxy-super-secret"
    proxy = ProxySettings("http://proxy.example:8080", "private-user", proxy_secret)
    manager = SessionManager(FakeFactory(), ttl_seconds=1)
    session_key = key(session_secret, site_secret, proxy)

    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        async with manager.acquire(session_key, proxy):
            pass
        assert await manager.reset(session_secret) == 1
        await manager.close()

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    serialized = "\n".join(str(record.__dict__) for record in caplog.records)
    for secret in (session_secret, site_secret, proxy_secret, "private-user"):
        assert secret not in rendered
        assert secret not in serialized
    assert any(hasattr(record, "session_digest") for record in caplog.records)


@pytest.mark.asyncio
async def test_lifecycle_logs_publish_bounded_pool_state_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager(FakeFactory(), max_sessions=2)
    session_key = key("private-account", "example.com")

    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        async with manager.acquire(session_key, ProxySettings.direct()):
            pass
        await manager.close()

    records: dict[str, dict[str, object]] = {
        str(record.__dict__.get("event", "")): record.__dict__
        for record in caplog.records
        if record.getMessage().startswith("browser_session_lifecycle ")
    }

    def count(event: str, field: str) -> int:
        value = records[event][field]
        assert isinstance(value, int)
        return value

    assert {"create", "idle", "shutdown"}.issubset(records)
    assert count("create", "active_count") == 1
    assert count("create", "busy_count") == 1
    assert count("create", "idle_count") == 0
    assert count("idle", "active_count") == 1
    assert count("idle", "busy_count") == 0
    assert count("idle", "idle_count") == 1
    assert count("shutdown", "active_count") == 0
    assert count("shutdown", "retiring_count") == 1
    for event in records:
        assert 0 <= count(event, "active_count") <= 2
        assert 0 <= count(event, "idle_count") <= 2
        assert 0 <= count(event, "busy_count") <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("log_level", [logging.INFO, logging.DEBUG])
async def test_production_logger_renders_safe_lifecycle_state_at_configured_level(
    monkeypatch: pytest.MonkeyPatch,
    log_level: int,
) -> None:
    """The shipped text logger exposes safe identity and pool state fields."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(production_logger, "handlers", [handler])
    monkeypatch.setattr(production_logger, "propagate", False)
    monkeypatch.setattr(production_logger, "level", log_level)
    session_secret = "session-never-render"
    proxy = ProxySettings(
        "http://proxy.example:8080", "proxy-user-never-render", "proxy-secret"
    )
    manager = SessionManager(FakeFactory(), max_sessions=2)
    session_key = key(session_secret, "reader.example.com", proxy)

    async with manager.acquire(session_key, proxy):
        pass
    async with manager.acquire(session_key, proxy):
        pass
    assert await manager.reset(session_secret) == 1
    await manager.close()

    rendered = stream.getvalue()
    assert "browser_session_lifecycle event=create" in rendered
    assert "browser_session_lifecycle event=reuse" in rendered
    assert "active_count=1" in rendered
    assert "idle_count=1" in rendered
    assert "busy_count=1" in rendered
    assert "capacity_limit=2" in rendered
    assert "session_digest=" in rendered
    assert "site_digest=" in rendered
    assert "proxy_digest=" in rendered
    for secret in (
        session_secret,
        "reader.example.com",
        "proxy-user-never-render",
        "proxy-secret",
    ):
        assert secret not in rendered
