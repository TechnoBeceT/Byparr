from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import verify_runtime as verifier
from scripts.verify_runtime import verify_runtime

PROJECT_ROOT = Path(__file__).parents[1]


def test_docker_build_fetches_an_immutable_verified_camoufox_runtime() -> None:
    """The image downloads the known working browser by immutable identity."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "camoufox fetch" not in dockerfile
    assert (
        "https://github.com/daijro/camoufox/releases/download/"
        "v135.0.1-beta.24/camoufox-135.0.1-beta.24-lin.x86_64.zip"
    ) in dockerfile
    assert (
        "61e1ec455e021720af38a5cc5ff7566121363cb5b82b72f24e381ba2676a4888" in dockerfile
    )
    assert (
        "d3999a025212c4fe8ecce8b799912fdf8bd12ca6a5062c87709056041a20c767" in dockerfile
    )


def test_docker_build_supplies_a_verified_read_only_geoip_database() -> None:
    """The image prepackages immutable GeoIP data for the runtime user."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "GeoLite2-City.mmdb" in dockerfile
    assert "GEOIP_DATABASE_SHA256" in dockerfile
    assert "install -m 0444" in dockerfile


def test_shipped_environment_launches_geoip_browser_as_uid_1000() -> None:
    """The test image exercises GeoIP browser launch under its runtime UID."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    test_stage = dockerfile.split("FROM app AS test", 1)[1].split("FROM app", 1)[0]
    assert "USER 1000" in test_stage
    assert "python -m scripts.verify_runtime_launch" in test_stage


@pytest.mark.parametrize(
    ("dependency", "declaration", "version"),
    [
        ("camoufox", "camoufox[geoip]==0.4.11", "0.4.11"),
        ("playwright", "playwright==1.58.0", "1.58.0"),
        ("playwright-captcha", "playwright-captcha==0.1.1", "0.1.1"),
        (
            "apify-fingerprint-datapoints",
            "apify-fingerprint-datapoints==0.10.0",
            "0.10.0",
        ),
    ],
)
def test_solver_dependency_closure_is_exactly_pinned(
    dependency: str, declaration: str, version: str
) -> None:
    """The Python solver dependency closure matches the known working runtime."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock = (PROJECT_ROOT / "uv.lock").read_text()

    assert f'"{declaration}"' in pyproject
    assert f'name = "{dependency}"' in lock
    assert f'version = "{version}"' in lock


def test_python_runtime_is_exactly_pinned_for_build_and_metadata() -> None:
    """The image and project metadata select the proven interpreter patch."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock = (PROJECT_ROOT / "uv.lock").read_text()

    assert "ARG PYTHON_VERSION=3.14.2" in dockerfile
    assert 'requires-python = "==3.14.2"' in pyproject
    assert 'requires-python = "==3.14.2"' in lock


def test_runtime_verifier_rejects_wrong_browser_identity(tmp_path: Path) -> None:
    """The verifier rejects a browser binary with an unexpected digest."""
    browser_dir = tmp_path / "camoufox"
    browser_dir.mkdir()
    (browser_dir / "version.json").write_text(
        json.dumps({"version": "135.0.1", "release": "beta.24"})
    )
    (browser_dir / "camoufox-bin").write_bytes(b"not the approved browser")

    with pytest.raises(RuntimeError, match="Camoufox executable checksum"):
        verify_runtime(
            browser_dir=browser_dir,
            geoip_database=tmp_path / "missing.mmdb",
            installed_versions={
                "camoufox": "0.4.11",
                "playwright": "1.58.0",
                "playwright-captcha": "0.1.1",
                "apify-fingerprint-datapoints": "0.10.0",
            },
        )


def test_runtime_verifier_rejects_wrong_python_version() -> None:
    """The verifier rejects an interpreter outside the proven runtime."""
    with pytest.raises(RuntimeError, match="Python runtime version"):
        verify_runtime(
            browser_dir=Path("/unused"),
            python_version="3.14.3",
        )


def test_runtime_verifier_rejects_wrong_camoufox_package_version() -> None:
    """The verifier rejects drift in the Camoufox Python package."""
    with pytest.raises(RuntimeError, match="solver dependency closure"):
        verify_runtime(
            browser_dir=Path("/unused"),
            python_version="3.14.2",
            installed_versions={
                "camoufox": "0.4.12",
                "playwright": "1.58.0",
                "playwright-captcha": "0.1.1",
                "apify-fingerprint-datapoints": "0.10.0",
            },
        )


def test_runtime_verifier_accepts_the_proven_runtime_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier accepts the complete known working runtime identity."""
    browser_dir = tmp_path / "camoufox"
    browser_dir.mkdir()
    (browser_dir / "version.json").write_text(
        json.dumps({"version": "135.0.1", "release": "beta.24"})
    )
    (browser_dir / "camoufox-bin").write_bytes(b"approved")
    geoip_database = tmp_path / "GeoLite2-City.mmdb"
    geoip_database.write_bytes(b"approved geoip")
    monkeypatch.setattr(
        verifier,
        "sha256_file",
        lambda path: (
            verifier.GEOIP_DATABASE_SHA256
            if path == geoip_database
            else verifier.CAMOUFOX_BINARY_SHA256
        ),
    )

    verifier.verify_runtime(
        browser_dir=browser_dir,
        geoip_database=geoip_database,
        installed_versions={
            "camoufox": "0.4.11",
            "playwright": "1.58.0",
            "playwright-captcha": "0.1.1",
            "apify-fingerprint-datapoints": "0.10.0",
        },
    )


def test_runtime_verifier_rejects_wrong_geoip_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier rejects GeoIP data with an unexpected digest."""
    browser_dir = tmp_path / "camoufox"
    browser_dir.mkdir()
    (browser_dir / "version.json").write_text(
        json.dumps({"version": "135.0.1", "release": "beta.24"})
    )
    (browser_dir / "camoufox-bin").write_bytes(b"approved")
    geoip_database = tmp_path / "GeoLite2-City.mmdb"
    geoip_database.write_bytes(b"not the approved database")
    monkeypatch.setattr(
        verifier,
        "sha256_file",
        lambda path: (
            verifier.CAMOUFOX_BINARY_SHA256
            if path == browser_dir / "camoufox-bin"
            else "wrong"
        ),
    )

    with pytest.raises(RuntimeError, match="GeoIP database checksum"):
        verifier.verify_runtime(
            browser_dir=browser_dir,
            geoip_database=geoip_database,
            installed_versions={
                "camoufox": "0.4.11",
                "playwright": "1.58.0",
                "playwright-captcha": "0.1.1",
                "apify-fingerprint-datapoints": "0.10.0",
            },
        )
