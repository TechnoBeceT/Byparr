from __future__ import annotations

import time
from http.client import INTERNAL_SERVER_ERROR
from typing import Any, Literal

from playwright.sync_api import Cookie
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from src import consts

MS_PER_SECOND = 1000
SESSION_PRINTABLE_MESSAGE = "session must contain printable characters only"
SESSION_GENERATION_REQUIRED_MESSAGE = (
    "sessionGeneration is required for confirmed recovery"
)
SESSION_REQUIRED_MESSAGE = "session is required for session commands"


class LinkRequest(BaseModel):
    model_config = {"populate_by_name": True}

    cmd: Literal[
        "request.get",
        "sessions.create",
        "sessions.destroy",
        "sessions.recovery.prepare",
        "sessions.recovery.confirm",
    ] = Field(
        default="request.get",
        description="FlareSolverr-compatible request or session command.",
    )
    url: str = Field(pattern=r"^https?://", default="https://")
    max_timeout: int = Field(
        default=60,
        alias="maxTimeout",
        description=(
            "Maximum timeout for resolving the anti-bot challenge. Values below 1000 "
            "are treated as seconds; values of 1000 or more as milliseconds, matching "
            "FlareSolverr's maxTimeout parameter."
        ),
    )
    block_media: bool = Field(
        default=consts.BLOCK_MEDIA,
        alias="blockMedia",
        description="Block image, media, and font resources from loading.",
    )
    return_only_cookies: bool = Field(
        default=consts.RETURN_ONLY_COOKIES,
        alias="returnOnlyCookies",
        description="Return only cookies, skip the page HTML content in the response.",
    )
    session: str | None = Field(
        default=None,
        max_length=128,
        description="Optional FlareSolverr-compatible session name.",
    )

    session_generation: str | None = Field(
        default=None, alias="sessionGeneration", pattern=r"^[0-9a-f]{32}$"
    )

    @field_validator("session", mode="before")
    @classmethod
    def normalize_session(cls, value: object) -> object:
        """Normalize session names while keeping them safe for identifiers and logs."""
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.isprintable():
            raise ValueError(SESSION_PRINTABLE_MESSAGE)
        return normalized

    @field_validator("max_timeout")
    @classmethod
    def normalize_max_timeout(cls, value: int) -> int:
        """Normalize FlareSolverr-style millisecond values to seconds."""
        if value >= MS_PER_SECOND:
            return value // MS_PER_SECOND
        return value

    @model_validator(mode="after")
    def require_session_for_session_commands(self) -> LinkRequest:
        """Require the normalized session name for non-navigation commands."""
        if self.cmd != "request.get" and self.session is None:
            raise ValueError(SESSION_REQUIRED_MESSAGE)
        if self.cmd == "sessions.recovery.confirm" and self.session_generation is None:
            raise ValueError(SESSION_GENERATION_REQUIRED_MESSAGE)
        return self


class HealthcheckResponse(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    msg: str = "Byparr is working!"
    version: str = consts.VERSION
    user_agent: str


class Solution(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    url: str
    status: int
    cookies: list[Cookie] = []
    user_agent: str = ""
    headers: dict[str, Any] = {}
    response: str = ""
    content_type: str = Field(default="text/html", alias="contentType")


class LinkResponse(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    status: str = "ok"
    message: str
    solution: Solution
    start_timestamp: int
    end_timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    version: str = consts.VERSION

    @classmethod
    def invalid(cls, url: str) -> LinkResponse:
        """
        Return an invalid LinkResponse with default error values.

        This method is used to generate a response indicating an invalid request.
        """
        return cls(
            status="error",
            message="Invalid request",
            solution=Solution(url=url, status=INTERNAL_SERVER_ERROR),
            start_timestamp=int(time.time() * 1000),
        )


class SessionResponse(BaseModel):
    """FlareSolverr-compatible envelope for session commands."""

    model_config = {"alias_generator": to_camel, "populate_by_name": True}
    status: str = "ok"
    message: str
    session: str | None = None
    start_timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    end_timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    version: str = consts.VERSION


class RecoveryResponse(BaseModel):
    """Versioned acknowledgment of exact-name arrival fencing and browser closure."""

    protocol: Literal["fenced-drain-close-v1"] = "fenced-drain-close-v1"
    session: str
    generation: str
    previous_generation: str | None = Field(default=None, alias="previousGeneration")
    outcome: Literal["prepared", "drained-closed"]
