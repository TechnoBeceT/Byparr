"""Exercise the shipped GeoIP-enabled browser without writable package data."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import camoufox
from camoufox.locale import MMDB_FILE

from src.browser import BrowserFactory
from src.proxy import ProxySettings

RUNTIME_UID = 1000


async def verify_runtime_launch() -> None:
    """Launch and close the production browser under its unprivileged identity."""
    if os.getuid() != RUNTIME_UID:
        msg = f"Runtime launch test requires UID {RUNTIME_UID}, got {os.getuid()}"
        raise RuntimeError(msg)

    package_dir = Path(camoufox.__file__).resolve().parent  # noqa: ASYNC240
    if os.access(package_dir, os.W_OK):
        msg = f"Camoufox package directory is writable: {package_dir}"
        raise RuntimeError(msg)
    if MMDB_FILE.stat().st_mode & 0o222:
        msg = f"GeoIP database is writable: {MMDB_FILE}"
        raise RuntimeError(msg)

    database_before = MMDB_FILE.stat()
    with patch("camoufox.utils.public_ip", return_value="8.8.8.8"):
        resource = await BrowserFactory().open(ProxySettings.direct())
    try:
        if await resource.page.title() != "":
            msg = "Fresh browser page should be blank"
            raise RuntimeError(msg)
    finally:
        await resource.close()

    database_after = MMDB_FILE.stat()
    if (
        database_after.st_size != database_before.st_size
        or database_after.st_mtime_ns != database_before.st_mtime_ns
    ):
        msg = "GeoIP database changed during browser launch"
        raise RuntimeError(msg)


if __name__ == "__main__":
    asyncio.run(verify_runtime_launch())
