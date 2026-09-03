import asyncio

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright_captcha import CaptchaType, ClickSolver
from playwright_captcha.solvers.click.cloudflare.utils.detection import (
    CF_INTERSTITIAL_INDICATORS_SELECTORS,
)

from src.browser import is_fatal_browser_error
from src.utils import TimeoutTimer, logger

__all__ = [
    "CF_INTERSTITIAL_INDICATORS_SELECTORS",
    "challenge_present",
    "solve_challenge",
]


class ChallengeSolverError(RuntimeError):
    """A bounded, nonfatal failure reported by the challenge dependency."""


async def challenge_present(page: Page) -> bool:
    """Report whether the Cloudflare interstitial is up."""
    for selector in CF_INTERSTITIAL_INDICATORS_SELECTORS:
        try:
            if await page.locator(selector).count() == 0:
                continue
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            if is_fatal_browser_error(error):
                raise
            if "Execution context was destroyed" in str(error):
                return False
        return True
    return False


async def solve_challenge(page: Page, solver: ClickSolver, timer: TimeoutTimer) -> None:
    """Delegate challenge interaction to the Camoufox-aware click solver."""
    logger.info("Challenge detected, attempting to solve...")
    try:
        await asyncio.wait_for(
            solver.solve_captcha(
                captcha_container=page,
                captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                wait_checkbox_attempts=1,
                wait_checkbox_delay=0.5,
            ),
            timeout=timer.remaining(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if is_fatal_browser_error(error):
            raise
        raise ChallengeSolverError from error
    logger.debug("Challenge cleared.")
