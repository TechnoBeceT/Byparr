import logging
import sys
from pathlib import Path

from playwright_captcha.utils.camoufox_add_init_script.add_init_script import (
    get_addon_path,
)
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    version: str = "unknown"

    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8191

    block_media: bool = False
    return_only_cookies: bool = False
    owui_api_key: str | None = None
    browser_locale: str | None = None
    session_ttl_seconds: int = Field(default=900, ge=1)
    session_max_sessions: int = Field(default=8, ge=1)
    session_lifecycle_timeout_seconds: int = Field(default=120, ge=1)


settings = Settings()

LOG_LEVEL = logging.getLevelNamesMapping()[settings.log_level.upper()]
VERSION = settings.version.removeprefix("v")

PROXY_SERVER = settings.proxy_server
PROXY_USERNAME = settings.proxy_username
PROXY_PASSWORD = settings.proxy_password

HOST = settings.host
PORT = settings.port

BLOCK_MEDIA = settings.block_media
RETURN_ONLY_COOKIES = settings.return_only_cookies

OWUI_API_KEY = settings.owui_api_key
BROWSER_LOCALE = settings.browser_locale
SESSION_TTL_SECONDS = settings.session_ttl_seconds
SESSION_MAX_SESSIONS = settings.session_max_sessions
SESSION_LIFECYCLE_TIMEOUT_SECONDS = settings.session_lifecycle_timeout_seconds

ADDON_PATH = str(Path(get_addon_path()).absolute())
MAX_ATTEMPTS = sys.maxsize
