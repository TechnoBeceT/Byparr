# ruff: noqa: D102, D103, D107, PLR2004, S105

import asyncio
import logging
from collections import deque

import pytest
from pydantic import ValidationError

from src.consts import Settings
from src.proxy import ProxySettings
from src.session_key import SessionKey
from src.sessions import SessionCapacityError, SessionManager


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeResource:
    def __init__(self, name: str, *, close_error: BaseException | None = None) -> None:
        self.name = name
        self.page = f"page:{name}"
        self.context = f"context:{name}"
        self.close_error = close_error
        self.close_calls = 0
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


class FakeFactory:
    def __init__(self, outcomes=()) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[ProxySettings] = []
        self.open_started = asyncio.Event()
        self.allow_open: asyncio.Event | None = None
        self.resources: list[FakeResource] = []

    async def open(self, proxy: ProxySettings):
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


async def next_turn() -> None:
    """Yield through an event-loop callback without timing-based sleeps."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


def key(session: str, site: str, proxy: ProxySettings | None = None) -> SessionKey:
    selected = proxy or ProxySettings.direct()
    return SessionKey(session=session, site=site, proxy_id=selected.identity)


def test_session_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(session_ttl_seconds=0)
    with pytest.raises(ValidationError):
        Settings(session_max_sessions=0)


def test_session_settings_have_bounded_defaults() -> None:
    settings = Settings()

    assert settings.session_ttl_seconds == 900
    assert settings.session_max_sessions == 8


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

    with caplog.at_level(logging.DEBUG, logger="src.sessions"):
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
