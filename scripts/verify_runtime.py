from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from collections.abc import Mapping
from pathlib import Path

from camoufox.locale import MMDB_FILE

CAMOUFOX_VERSION = {"version": "135.0.1", "release": "beta.24"}
CAMOUFOX_BINARY_SHA256 = (
    "d3999a025212c4fe8ecce8b799912fdf8bd12ca6a5062c87709056041a20c767"
)
PYTHON_VERSION = "3.14.2"
SOLVER_VERSIONS = {
    "camoufox": "0.4.11",
    "playwright": "1.58.0",
    "playwright-captcha": "0.1.1",
    "apify-fingerprint-datapoints": "0.10.0",
}
GEOIP_DATABASE_SHA256 = (
    "ffedb2751cae16fdd886814a6c8480633c915ec136c1582ca5c9fc5861f781aa"
)


def sha256_file(path: Path) -> str:
    """Calculate the SHA-256 digest of a runtime artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_runtime(
    *,
    browser_dir: Path = Path("/cache/camoufox"),
    geoip_database: Path | None = None,
    installed_versions: Mapping[str, str] | None = None,
    python_version: str | None = None,
) -> None:
    """Reject a runtime whose interpreter, browser, or solver closure drifted."""
    actual_python = python_version or platform.python_version()
    if actual_python != PYTHON_VERSION:
        message = f"Unexpected Python runtime version: {actual_python}"
        raise RuntimeError(message)

    versions = installed_versions or {
        dependency: importlib.metadata.version(dependency)
        for dependency in SOLVER_VERSIONS
    }
    if dict(versions) != SOLVER_VERSIONS:
        message = f"Unexpected solver dependency closure: {dict(versions)!r}"
        raise RuntimeError(message)

    version = json.loads((browser_dir / "version.json").read_text())
    if version != CAMOUFOX_VERSION:
        message = f"Unexpected Camoufox version metadata: {version!r}"
        raise RuntimeError(message)

    binary_sha = sha256_file(browser_dir / "camoufox-bin")
    if binary_sha != CAMOUFOX_BINARY_SHA256:
        message = f"Unexpected Camoufox executable checksum: {binary_sha}"
        raise RuntimeError(message)

    if geoip_database is None:
        geoip_database = MMDB_FILE
    geoip_sha = sha256_file(geoip_database)
    if geoip_sha != GEOIP_DATABASE_SHA256:
        message = f"Unexpected GeoIP database checksum: {geoip_sha}"
        raise RuntimeError(message)


if __name__ == "__main__":
    verify_runtime()
