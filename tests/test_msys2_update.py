"""Cover stable MSYS2 SFX release discovery and package integration settings.

The GitHub release feed boundary is mocked, while the package-local update
module and its checked-in manifest are real. Network downloads and Windows SFX
execution are outside this suite's scope.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tomllib
from pathlib import Path
from unittest import mock

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "pkgs" / "msys2" / "vbootstrap.l1" / "pkg.local" / "check_update.py"
UNPACKER = ROOT / "pkgs" / "msys2" / "vbootstrap.l1" / "pkg.local" / "unpack_app.py"
MANIFEST = CHECKER.parents[1] / "pkg.toml"


def _load_checker():
    """Load the MSYS2 package-local checker as the update coordinator does."""
    spec = importlib.util.spec_from_file_location("msys2_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_unpacker():
    """Load the MSYS2 package-local unpacker as the payload coordinator does."""
    spec = importlib.util.spec_from_file_location("msys2_unpacker", UNPACKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_uses_the_newest_stable_x86_64_sfx_asset() -> None:
    """A dated stable release selects its matching self-extracting archive."""
    checker = _load_checker()
    releases = [
        {
            "tag_name": "nightly-x86_64",
            "assets": [
                {
                    "name": "msys2-base-x86_64-latest.sfx.exe",
                    "browser_download_url": "https://example.invalid/nightly.sfx.exe",
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        },
        {
            "tag_name": "2026-06-11",
            "assets": [
                {
                    "name": "msys2-base-x86_64-20260611.sfx.exe",
                    "browser_download_url": "https://example.invalid/msys2.sfx.exe",
                    "digest": "sha256:" + "b" * 64,
                }
            ],
        },
    ]

    with mock.patch.object(
        checker.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(releases).encode())
    ):
        candidate = checker.check_update({"current": {"version": "bootstrap"}})

    assert candidate == {
        "candidateId": "msys2:2026-06-11:msys2-base-x86_64-20260611.sfx.exe",
        "version": "20260611",
        "url": "https://example.invalid/msys2.sfx.exe",
        "fileName": "msys2-base-x86_64-20260611.sfx.exe",
        "sha256": "b" * 64,
    }


def test_bootstrap_manifest_declares_msys2_install_path() -> None:
    """Installing MSYS2 writes its selected App directory to the expected variable."""
    version_path = MANIFEST.parent
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )

    config = normalize_runtime_config(
        tomllib.loads(MANIFEST.read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "module"
    assert config["environment"] == [{"Name": "MSYS2_INSTALL_PATH", "Value": "$App"}]


def test_unpacker_promotes_msys64_contents_to_the_app_root(tmp_path: Path) -> None:
    """The staged App directory directly contains the extracted MSYS2 runtime."""
    unpacker = _load_unpacker()
    stage_app = tmp_path / "App"

    def extract_sfx(*args, **kwargs) -> None:
        extracted_root = stage_app / "msys64"
        (extracted_root / "usr" / "bin").mkdir(parents=True)
        (extracted_root / "msys2.exe").write_text("", encoding="utf-8")
        (extracted_root / "usr" / "bin" / "env.exe").write_text("", encoding="utf-8")

    with mock.patch.object(unpacker.subprocess, "run", side_effect=extract_sfx):
        unpacker.unpack_app(
            {"paths": {"artifact": tmp_path / "msys2.sfx.exe", "stageApp": stage_app}}
        )

    assert (stage_app / "msys2.exe").is_file()
    assert (stage_app / "usr" / "bin" / "env.exe").is_file()
    assert not (stage_app / "msys64").exists()
