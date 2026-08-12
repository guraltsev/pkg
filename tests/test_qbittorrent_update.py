"""Cover qBittorrent installer release discovery and staged extraction.

The SourceForge response and installer process boundary are mocked, while the
package-local update modules and runtime manifest normalization are real.
Network transport, SourceForge's redirect target, and qBittorrent installer
internals are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


PACKAGE = ROOT / "pkgs" / "qbittorrent" / "v5.2.3.l1"
CHECKER = PACKAGE / "pkg.local" / "check_update.py"
UNPACKER = PACKAGE / "pkg.local" / "unpack_app.py"
POPULATOR = PACKAGE / "pkg.local" / "populate_app.py"


def _load_module(path: Path, name: str):
    """Load a package-local update module without installing its package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_reads_the_windows_installer_from_release_metadata() -> None:
    """Windows release metadata supplies a newer 64-bit release candidate."""
    checker = _load_module(CHECKER, "qbittorrent_checker")
    metadata = {
        "platform_releases": {
            "windows": {
                "filename": (
                    "/qbittorrent-win32/qbittorrent-5.2.3/"
                    "qbittorrent_5.2.3_x64_setup.exe"
                )
            }
        }
    }

    with mock.patch.object(
        checker.urllib.request,
        "urlopen",
        return_value=io.BytesIO(json.dumps(metadata).encode("utf-8")),
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "5.2.2"}})

    assert candidate == {
        "candidateId": "qbittorrent:5.2.3:qbittorrent_5.2.3_x64_setup.exe",
        "version": "5.2.3",
        "url": (
            "https://sourceforge.net/projects/qbittorrent/files/"
            "qbittorrent-win32/qbittorrent-5.2.3/"
            "qbittorrent_5.2.3_x64_setup.exe/download"
        ),
        "fileName": "qbittorrent_5.2.3_x64_setup.exe",
    }
    assert urlopen.call_args.args[0].full_url == checker._RELEASE_METADATA


def test_unpacker_extracts_the_installer_into_the_staged_app_directory(tmp_path) -> None:
    """The installer is unpacked directly into the staged App path."""
    unpacker = _load_module(UNPACKER, "qbittorrent_unpacker")
    artifact = tmp_path / "qbittorrent_5.2.3_x64_setup.exe"
    stage_app = tmp_path / "stage" / "App"

    with mock.patch.object(unpacker.subprocess, "run") as run:
        unpacker.unpack_app(
            {"paths": {"artifact": artifact, "stageApp": stage_app}}
        )

    assert stage_app.is_dir()
    command = run.call_args.args[0]
    assert command.startswith("7z x -y ")
    assert f"-o{stage_app}" in command
    assert str(artifact) in command
    assert run.call_args.kwargs == {"check": True, "shell": True}


def test_populator_extracts_the_downloaded_installer_into_app(tmp_path) -> None:
    """Initial population downloads and extracts the installer without running NSIS."""
    populator = _load_module(POPULATOR, "qbittorrent_populator")
    app = tmp_path / "App"

    with mock.patch.object(
        populator.urllib.request, "urlopen", return_value=io.BytesIO(b"setup")
    ) as urlopen:
        with mock.patch.object(populator.subprocess, "run") as run:
            populator.populate_app(
                {"identity": {"version": "5.2.3"}, "PkgVars": {"App": str(app)}}
            )

    assert app.is_dir()
    assert urlopen.call_args.args[0].endswith(
        "qbittorrent_5.2.3_x64_setup.exe/download"
    )
    command = run.call_args.args[0]
    assert command.startswith("7z x -y ")
    assert f"-o{app}" in command
    assert command.endswith("qbittorrent_5.2.3_x64_setup.exe")
    assert run.call_args.kwargs == {"check": True, "shell": True}


def test_manifest_uses_package_local_installer_update_modules() -> None:
    """The qBittorrent manifest bootstraps and updates with local modules."""
    identity = PackageIdentity.from_version_path(PACKAGE.parent, PACKAGE, is_current=False)
    config = normalize_runtime_config(
        tomllib.loads((PACKAGE / "pkg.toml").read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "module"
    assert config["origin"]["mode"] == "module"
    assert config["origin"]["module"] == "pkg.local/populate_app.py"
    assert (PACKAGE / config["origin"]["module"]).is_file()
