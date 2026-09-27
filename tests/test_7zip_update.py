"""Cover 7-Zip release discovery and staged executable extraction.

The official download-page response and extraction process boundary are
mocked, while the package-local update modules and runtime manifest
normalization are real. Network transport, GitHub release redirects, and
7-Zip archive internals are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tomllib
from pathlib import Path
from unittest import mock

from tests.runtime_paths import find_runtime_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(find_runtime_directory(ROOT)))

from gupkg import extractors
from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


PACKAGE = ROOT / "pkgs" / "7zip" / "vbootstrap.l1"
CHECKER = PACKAGE / "pkg.local" / "check_update.py"
UNPACKER = PACKAGE / "pkg.local" / "unpack_app.py"


def _load_module(path: Path, name: str):
    """Load a package-local update module without installing its package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_selects_the_announced_x64_executable() -> None:
    """The official x64 announcement supplies the versioned executable candidate."""
    checker = _load_module(CHECKER, "seven_zip_checker")
    page = b"""
        <p>Download 7-Zip 26.03 (2026-09-03) for Windows x64 (64-bit):</p>
        <a href="https://github.com/ip7z/7zip/releases/download/26.03/7z2603-x64.exe">Download</a>
        <a href="https://github.com/ip7z/7zip/releases/download/26.03/7z2603.exe">Download</a>
        <a href="https://github.com/ip7z/7zip/releases/download/26.03/7z2603-arm64.exe">Download</a>
    """

    with mock.patch.object(
        checker.urllib.request, "urlopen", return_value=io.BytesIO(page)
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "26.02"}})

    assert candidate == {
        "candidateId": "7zip:26.03:7z2603-x64.exe",
        "version": "26.03",
        "url": "https://github.com/ip7z/7zip/releases/download/26.03/7z2603-x64.exe",
        "fileName": "7z2603-x64.exe",
    }
    assert urlopen.call_args.args[0] == checker._DOWNLOAD_PAGE


def test_unpacker_extracts_the_executable_into_the_staged_app_directory(tmp_path) -> None:
    """The downloaded self-extracting archive is unpacked only into staged App."""
    unpacker = _load_module(UNPACKER, "seven_zip_unpacker")
    artifact = tmp_path / "7z2603-x64.exe"
    stage_app = tmp_path / "stage" / "App"

    with mock.patch.object(unpacker.subprocess, "run") as run:
        unpacker.unpack_app({"paths": {"artifact": artifact, "stageApp": stage_app}})

    assert stage_app.is_dir()
    assert run.call_args.args[0] == [
        str(extractors.find_7z()),
        "x",
        "-y",
        f"-o{stage_app}",
        str(artifact),
    ]
    assert run.call_args.kwargs == {"check": True}


def test_bootstrap_manifest_uses_local_discovery_and_extraction_modules() -> None:
    """The checked-in bootstrap manifest enables the 7-Zip update workflow."""
    identity = PackageIdentity.from_version_path(PACKAGE.parent, PACKAGE, is_current=False)
    config = normalize_runtime_config(
        tomllib.loads((PACKAGE / "pkg.toml").read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "module"
    assert config["update"]["payload"]["ignore_checksum"] is True
    assert config["shortcut"][0]["targetPath"] == "$App\\7zFM.exe"
    assert config["bin"][0]["target"] == "$App\\7z.exe"
