"""Cover FFmpeg release discovery from Gyan.dev's publisher endpoints.

The HTTP responses are mocked, while the package-local update module and
manifest normalization are real. Archive downloading, checksum enforcement,
and ZIP extraction are handled by the package manager and are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import tomllib
from pathlib import Path
from unittest import mock

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "pkgs" / "ffmpeg" / "vbootstrap"
CHECKER = PACKAGE / "pkg.local" / "check_update.py"


def _load_checker():
    """Load the FFmpeg package-local checker as the update coordinator does."""
    spec = importlib.util.spec_from_file_location("ffmpeg_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_returns_release_essentials_zip_with_publisher_checksum() -> None:
    """The publisher endpoints supply a verified release essentials ZIP candidate."""
    checker = _load_checker()
    digest = "0123456789abcdef" * 4

    with mock.patch.object(
        checker.urllib.request,
        "urlopen",
        side_effect=[io.BytesIO(b"9.0.2\n"), io.BytesIO(f"{digest}\n".encode())],
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "9.0.1"}})

    assert candidate == {
        "candidateId": "ffmpeg:9.0.2:ffmpeg-release-essentials.zip",
        "version": "9.0.2",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "fileName": "ffmpeg-release-essentials.zip",
        "sha256": digest,
    }
    assert urlopen.call_args_list[0].args[0] == checker._VERSION_URL
    assert urlopen.call_args_list[1].args[0] == checker._CHECKSUM_URL


def test_checker_skips_a_healthy_current_release() -> None:
    """A healthy installed release does not request a redundant package update."""
    checker = _load_checker()

    with mock.patch.object(
        checker.urllib.request, "urlopen", return_value=io.BytesIO(b"9.0.2\n")
    ) as urlopen:
        candidate = checker.check_update(
            {"current": {"version": "9.0.2", "appReady": True}}
        )

    assert candidate is None
    assert urlopen.call_count == 1


def test_bootstrap_manifest_exposes_the_ffmpeg_command_line_tools() -> None:
    """The bootstrap package stages the essentials binaries and exposes their commands."""
    identity = PackageIdentity.from_version_path(
        PACKAGE.parent, PACKAGE, is_current=False
    )
    config = normalize_runtime_config(
        tomllib.loads((PACKAGE / "pkg.toml").read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["extract"] == [
        {"src": "ffmpeg-*-essentials_build/bin/", "dest": ""}
    ]
    assert [item["target"] for item in config["bin"]] == [
        "$App\\ffmpeg.exe",
        "$App\\ffprobe.exe",
        "$App\\ffplay.exe",
    ]
