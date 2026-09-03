# ruff: noqa: D102

from unittest.mock import AsyncMock

import pytest

from src.challenge import solve_challenge
from src.utils import TimeoutTimer


class PersistentChallengeLocator:
    async def count(self) -> int:
        return 1


class PersistentChallengePage:
    def locator(self, _selector: str) -> PersistentChallengeLocator:
        return PersistentChallengeLocator()


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
