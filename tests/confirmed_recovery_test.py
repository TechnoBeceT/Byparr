# ruff: noqa: PLR2004
"""Confirmed recovery exercises retained browser ownership and arrival fencing."""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src import endpoints
from src.models import LinkResponse, Solution
from src.proxy import ProxySettings
from src.sessions import SessionCapacityError, SessionManager, SessionResetError
from tests.sessions_test import FakeFactory, FakeResource, key


@pytest.mark.asyncio
async def test_confirmation_drains_closes_and_fences_delayed_old_request():
    """Verify confirmation drains closes and fences delayed old request at the ownership boundary."""
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    resource = FakeResource("old", close_started=close_started, allow_close=allow_close)
    factory = FakeFactory([resource])
    manager = SessionManager(factory)
    generation = await manager.prepare_recovery("same-name")
    entered, release = asyncio.Event(), asyncio.Event()

    async def active() -> None:
        async with manager.acquire(
            key("same-name", "one.example"), ProxySettings(), generation=generation
        ):
            entered.set()
            await release.wait()

    old = asyncio.create_task(active())
    await entered.wait()
    recovery = asyncio.create_task(manager.confirm_recovery("same-name", generation))
    await asyncio.sleep(0)
    assert not recovery.done()
    with pytest.raises(SessionResetError):
        async with manager.acquire(
            key("same-name", "two.example"), ProxySettings(), generation=generation
        ):
            pytest.fail("old arrival acquired browser")
    release.set()
    await old
    await close_started.wait()
    assert not recovery.done(), "confirmation preceded browser close"
    allow_close.set()
    replacement = await recovery
    assert replacement != generation
    assert resource.close_calls == 1
    with pytest.raises(SessionResetError):
        async with manager.acquire(
            key("same-name", "one.example"), ProxySettings(), generation=generation
        ):
            pytest.fail("delayed old request entered replacement")
    with pytest.raises(SessionResetError):
        async with manager.acquire(key("same-name", "one.example"), ProxySettings()):
            pytest.fail("untagged old request entered replacement")
    async with manager.acquire(
        key("same-name", "one.example"), ProxySettings(), generation=replacement
    ):
        pass
    assert len(factory.calls) == 2
    assert await manager.confirm_recovery("same-name", generation) == replacement
    await manager.close()


@pytest.mark.asyncio
async def test_close_failure_never_acknowledges_or_admits_replacement():
    """Verify close failure never acknowledges or admits replacement at the ownership boundary."""
    factory = FakeFactory(
        [FakeResource("old", close_error=RuntimeError("close failed"))]
    )
    manager = SessionManager(factory)
    generation = await manager.prepare_recovery("same-name")
    async with manager.acquire(
        key("same-name", "one.example"), ProxySettings(), generation=generation
    ):
        pass
    with pytest.raises(RuntimeError, match="close failed"):
        await manager.confirm_recovery("same-name", generation)
    with pytest.raises(SessionResetError):
        await manager.prepare_recovery("same-name")
    assert len(factory.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_second_recovery_cycle_and_concurrent_confirm_are_idempotent():
    """Verify second recovery cycle and concurrent confirm are idempotent at the ownership boundary."""
    manager = SessionManager(FakeFactory())
    first = await manager.prepare_recovery("same-name")
    second, joined = await asyncio.gather(
        manager.confirm_recovery("same-name", first),
        manager.confirm_recovery("same-name", first),
    )
    assert second == joined
    assert await manager.confirm_recovery("same-name", first) == second
    third = await manager.confirm_recovery("same-name", second)
    assert third not in (first, second)
    with pytest.raises(SessionResetError):
        await manager.confirm_recovery("same-name", first)
    await manager.close()


@pytest.mark.asyncio
async def test_retired_close_failure_retains_exact_name_evidence():
    """Verify retired close failure retains exact name evidence at the ownership boundary."""
    factory = FakeFactory(
        [FakeResource("old", close_error=RuntimeError("close failed"))]
    )
    manager = SessionManager(factory)
    old = key("failed-name", "one.example")
    async with manager.acquire(old, ProxySettings()):
        pass
    await manager.invalidate(old)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(SessionResetError):
        await manager.prepare_recovery("failed-name")
    assert await manager.prepare_recovery("other-name")
    await manager.close()


@pytest.mark.asyncio
async def test_http_generation_gate_and_confirmation_envelope(monkeypatch):
    """Verify http generation gate and confirmation envelope at the ownership boundary."""
    factory = FakeFactory()
    manager = SessionManager(factory)
    app = FastAPI()
    app.include_router(endpoints.router)
    app.state.session_manager = manager

    async def navigate(request, _browser) -> LinkResponse:
        return LinkResponse(
            message="fixture",
            solution=Solution(url=request.url, status=200),
            start_timestamp=1,
        )

    monkeypatch.setattr(endpoints, "read_item", navigate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://solver"
    ) as client:
        assert (await client.get("/v1/session-recovery")).json() == {
            "protocol": "fenced-drain-close-v1"
        }
        prepared = (
            await client.post(
                "/v1", json={"cmd": "sessions.recovery.prepare", "session": "same-name"}
            )
        ).json()
        generation = prepared["generation"]
        request = {
            "cmd": "request.get",
            "url": "https://reader.example/chapter",
            "session": "same-name",
        }
        assert (await client.post("/v1", json=request)).status_code == 409
        assert len(factory.calls) == 0
        assert (
            await client.post(
                "/v1", json=request, headers={"X-Byparr-Session-Generation": generation}
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1", json={"cmd": "sessions.destroy", "session": "same-name"}
            )
        ).status_code == 409
        confirmed = (
            await client.post(
                "/v1",
                json={
                    "cmd": "sessions.recovery.confirm",
                    "session": "same-name",
                    "sessionGeneration": generation,
                },
            )
        ).json()
        assert confirmed["protocol"] == "fenced-drain-close-v1"
        assert confirmed["outcome"] == "drained-closed"
        assert confirmed["session"] == "same-name"
        assert confirmed["previousGeneration"] == generation
        assert confirmed["generation"] != generation
        assert factory.resources[0].closed.is_set()
        assert (
            await client.post(
                "/v1", json=request, headers={"X-Byparr-Session-Generation": generation}
            )
        ).status_code == 409
        assert len(factory.calls) == 1
        assert (
            await client.post(
                "/v1",
                json=request,
                headers={"X-Byparr-Session-Generation": confirmed["generation"]},
            )
        ).status_code == 200
        assert len(factory.calls) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_fences_are_bounded_and_incarnation_cannot_confirm_old_token():
    """Verify fences are bounded and incarnation cannot confirm old token at the ownership boundary."""
    manager = SessionManager(FakeFactory())
    tokens = [await manager.prepare_recovery(f"name-{number}") for number in range(128)]

    with pytest.raises(SessionCapacityError):
        await manager.prepare_recovery("overflow")
    assert await manager.prepare_recovery("name-0") == tokens[0]
    restarted = SessionManager(FakeFactory())
    with pytest.raises(SessionResetError):
        await restarted.confirm_recovery("name-0", tokens[0])
    assert await restarted.prepare_recovery("name-0") != tokens[0]
    await manager.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_managed_open_failure_never_acknowledges():
    """Failed browser creation cannot be mistaken for confirmed retirement."""
    manager = SessionManager(FakeFactory([RuntimeError("open failed")]))
    generation = await manager.prepare_recovery("same-name")
    with pytest.raises(RuntimeError, match="open failed"):
        async with manager.acquire(
            key("same-name", "reader.example"), ProxySettings(), generation=generation
        ):
            pytest.fail("failed creation yielded a browser")
    with pytest.raises(RuntimeError, match="open failed"):
        await manager.confirm_recovery("same-name", generation)
    await manager.close()


@pytest.mark.asyncio
async def test_confirm_closes_all_sites_and_proxies_only_for_exact_name():
    """Confirmed recreation retires shared cookies only for the named scope."""
    factory = FakeFactory()
    manager = SessionManager(factory)
    generation = await manager.prepare_recovery("same-name")
    proxy = ProxySettings("http://proxy.test:8080", "user", "password")
    async with manager.acquire(
        key("same-name", "one.example"), ProxySettings(), generation=generation
    ):
        pass
    async with manager.acquire(
        key("same-name", "two.example", proxy), proxy, generation=generation
    ):
        pass
    async with manager.acquire(key("other-name", "one.example"), ProxySettings()):
        pass
    await manager.confirm_recovery("same-name", generation)
    assert [resource.close_calls for resource in factory.resources] == [1, 1, 0]
    await manager.close()
