"""Cover VLC release discovery from VideoLAN's public directory index.

The VideoLAN HTTP responses are mocked, while the package-local update module
and manifest normalization are real. Archive downloading, checksum enforcement,
and ZIP extraction are handled by the package manager and are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import tomllib
from pathlib import Path
from unittest import mock

from pkg.configuration import normalize_runtime_config
from pkg.core import PackageIdentity


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "pkgs" / "vlc" / "vbootstrap.l1"
CHECKER = PACKAGE / "pkg.local" / "check_update.py"


def _load_checker():
    """Load the VLC package-local checker as the update coordinator does."""
    spec = importlib.util.spec_from_file_location("vlc_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_selects_the_newest_numeric_release_and_its_zip_checksum() -> None:
    """The highest numeric index folder supplies the 64-bit ZIP candidate."""
    checker = _load_checker()
    index = b"""
        <a href=\"3.0.22/\">3.0.22/</a>
        <a href=\"3.0.23/\">3.0.23/</a>
        <a href=\"last/\">last/</a>
        <a href=\"4.0.0-dev/\">4.0.0-dev/</a>
    """
    digest = "0123456789abcdef" * 4
    checksum = f"{digest}  vlc-3.0.23-win64.zip\n".encode()

    with mock.patch.object(
        checker.urllib.request,
        "urlopen",
        side_effect=[io.BytesIO(index), io.BytesIO(checksum)],
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "3.0.22"}})

    assert candidate == {
        "candidateId": "vlc:3.0.23:vlc-3.0.23-win64.zip",
        "version": "3.0.23",
        "url": (
            "https://download.videolan.org/pub/videolan/vlc/"
            "3.0.23/win64/vlc-3.0.23-win64.zip"
        ),
        "fileName": "vlc-3.0.23-win64.zip",
        "sha256": digest,
    }
    assert urlopen.call_args_list[0].args[0] == checker._RELEASE_INDEX
    assert urlopen.call_args_list[1].args[0].endswith(".zip.sha256")


def test_bootstrap_manifest_uses_the_local_zip_updater() -> None:
    """The checked-in manifest enables module discovery and ZIP extraction."""
    identity = PackageIdentity.from_version_path(
        PACKAGE.parent, PACKAGE, is_current=False
    )
    config = normalize_runtime_config(
        tomllib.loads((PACKAGE / "pkg.toml").read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "zip"
