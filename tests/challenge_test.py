# ruff: noqa: D102

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError

from src.challenge import challenge_present, solve_challenge
from src.utils import TimeoutTimer


class PersistentChallengeLocator:
    async def count(self) -> int:
        return 1


class PersistentChallengePage:
    def locator(self, _selector: str) -> PersistentChallengeLocator:
        return PersistentChallengeLocator()


def challenge_page(title: str | BaseException) -> MagicMock:
    """Model a page with a dependency marker and a selected title result."""
    page = MagicMock()
    marker = MagicMock()
    marker.count = AsyncMock(return_value=1)
    page.locator.return_value = marker
    if isinstance(title, BaseException):
        page.title = AsyncMock(side_effect=title)
    else:
        page.title = AsyncMock(return_value=title)
    return page


@pytest.mark.asyncio
async def test_dependency_marker_without_interstitial_title_is_not_a_challenge() -> (
    None
):
    """A solved page may retain a marker without remaining an interstitial."""
    assert not await challenge_present(challenge_page("The Blank"))


@pytest.mark.asyncio
async def test_dependency_marker_with_normalized_interstitial_title_is_a_challenge() -> (
    None
):
    """Incidental title casing and outer space do not hide an interstitial."""
    assert await challenge_present(challenge_page("  JUST A MOMENT...  "))


@pytest.mark.asyncio
async def test_fatal_title_read_error_escapes_challenge_detection() -> None:
    """Browser closure during the title probe remains a lifecycle signal."""
    with pytest.raises(TargetClosedError):
        await challenge_present(
            challenge_page(TargetClosedError("browser has been closed"))
        )


@pytest.mark.asyncio
async def test_destroyed_title_execution_context_is_not_a_challenge() -> None:
    """A navigation race does not manufacture a challenge from stale markup."""
    assert not await challenge_present(
        challenge_page(PlaywrightError("Execution context was destroyed"))
    )


@pytest.mark.asyncio
async def test_solver_return_is_success_even_if_challenge_marker_remains() -> None:
    """The solver owns challenge success; stale page markers cannot overturn it."""
    solver = AsyncMock()

    await solve_challenge(
        PersistentChallengePage(),
        solver,
        TimeoutTimer(duration=10),
    )

    solver.solve_captcha.assert_awaited_once()
