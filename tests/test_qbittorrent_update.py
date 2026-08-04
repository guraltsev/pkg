"""Cover qBittorrent installer release discovery and staged installation.

The SourceForge response and installer process boundary are mocked, while the
package-local update modules and runtime manifest normalization are real.
Network transport, SourceForge's redirect target, and qBittorrent installer
internals are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


PACKAGE = ROOT / "gupkgs" / "qbittorrent" / "v5.2.3.l1"
CHECKER = PACKAGE / "pkg.local" / "check_update.py"
UNPACKER = PACKAGE / "pkg.local" / "unpack_app.py"


def _load_module(path: Path, name: str):
    """Load a package-local update module without installing its package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_parses_the_latest_sourceforge_installer_filename() -> None:
    """The latest-download filename supplies a newer 64-bit release candidate."""
    checker = _load_module(CHECKER, "qbittorrent_checker")
    page = b"<main>qbittorrent_5.2.3_x64_setup.exe</main>"

    with mock.patch.object(
        checker.urllib.request, "urlopen", return_value=io.BytesIO(page)
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "5.2.2"}})

    assert candidate == {
        "candidateId": "qbittorrent:5.2.3:qbittorrent_5.2.3_x64_setup.exe",
        "version": "5.2.3",
        "url": "https://sourceforge.net/projects/qbittorrent/files/latest/download",
        "fileName": "qbittorrent_5.2.3_x64_setup.exe",
    }
    assert urlopen.call_args.args[0].full_url == checker._LATEST_DOWNLOAD


def test_unpacker_silently_installs_into_the_staged_app_directory(tmp_path) -> None:
    """The installer receives its silent destination as the staged App path."""
    unpacker = _load_module(UNPACKER, "qbittorrent_unpacker")
    artifact = tmp_path / "qbittorrent_5.2.3_x64_setup.exe"
    stage_app = tmp_path / "stage" / "App"

    with mock.patch.object(unpacker.subprocess, "run") as run:
        unpacker.unpack_app(
            {"paths": {"artifact": artifact, "stageApp": stage_app}}
        )

    assert stage_app.is_dir()
    run.assert_called_once_with(
        [str(artifact), "/S", f"/D={stage_app}"], check=True
    )


def test_manifest_uses_package_local_installer_update_modules() -> None:
    """The qBittorrent manifest enables the local check and unpack workflow."""
    identity = PackageIdentity.from_version_path(PACKAGE.parent, PACKAGE, is_current=False)
    config = normalize_runtime_config(
        tomllib.loads((PACKAGE / "pkg.toml").read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "module"
