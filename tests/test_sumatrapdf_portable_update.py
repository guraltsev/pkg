"""Cover SumatraPDF Portable release discovery and installed executable naming.

The SumatraPDF download page boundary is mocked, while the package-local update
module and manifest normalization are real. Downloading, ZIP extraction, and
the generic payload rename mechanism are covered by the package manager's
broader update tests and are out of scope.
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
CHECKER = (
    ROOT
    / "pkgs"
    / "SumatraPDF-portable"
    / "vbootstrap.l1"
    / "pkg.local"
    / "check_update.py"
)
MANIFEST = CHECKER.parents[1] / "pkg.toml"


def _load_checker():
    """Load the package-local checker as the update coordinator does."""
    spec = importlib.util.spec_from_file_location("sumatrapdf_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_returns_the_versioned_portable_zip_from_download_page() -> None:
    """The matching 64-bit anchor determines the candidate version and ZIP URL."""
    checker = _load_checker()
    page = b'''
        <tr>
          <td>Portable version:&nbsp;&nbsp;</td>
          <td><a href="/dl/rel/3.6.1/SumatraPDF-3.6.1-64.zip"
            onclick="return SetupRedirect();">SumatraPDF-3.6.1-64.zip</a></td>
        </tr>
        <td><a href="/dl/rel/3.6.1/SumatraPDF-3.6.1-arm64.zip">Arm portable</a></td>
        <td><a href="/dl/rel/3.6.1/SumatraPDF-3.6.1.zip">32-bit portable</a></td>
    '''

    with mock.patch.object(
        checker.urllib.request, "urlopen", return_value=io.BytesIO(page)
    ) as urlopen:
        candidate = checker.check_update({"current": {"version": "3.5.2"}})

    assert candidate == {
        "candidateId": "sumatrapdf:3.6.1:SumatraPDF-3.6.1-64.zip",
        "version": "3.6.1",
        "url": "https://www.sumatrapdfreader.org/dl/rel/3.6.1/SumatraPDF-3.6.1-64.zip",
        "fileName": "SumatraPDF-3.6.1-64.zip",
        "headers": {"User-Agent": checker._DOWNLOAD_USER_AGENT},
    }
    assert urlopen.call_args.args[0].full_url == (
        "https://www.sumatrapdfreader.org/download-free-pdf-viewer"
    )


def test_manifest_renames_the_release_executable_to_a_stable_name(tmp_path) -> None:
    """The installed shortcut and native shim use the versionless executable."""
    identity = PackageIdentity.from_version_path(
        tmp_path / "SumatraPDF-portable",
        tmp_path / "SumatraPDF-portable" / "v3.6.1.l1",
        is_current=False,
    )

    config = normalize_runtime_config(
        tomllib.loads(MANIFEST.read_text(encoding="utf-8")), identity
    )

    assert config["update"]["payload"]["rename"] == [
        {"src": "SumatraPDF-${version}-64.exe", "dest": "SumatraPDF.exe"}
    ]
    assert config["shortcut"][0]["targetPath"] == "$App\\SumatraPDF.exe"
    assert config["bin"][0]["target"] == "$App\\SumatraPDF.exe"
    assert config["bin"][0]["type"] == "gui"


def test_bootstrap_manifest_uses_the_local_zip_updater() -> None:
    """The checked-in manifest activates the module check and ZIP payload flow."""
    version_path = MANIFEST.parent
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )

    config = normalize_runtime_config(
        tomllib.loads(MANIFEST.read_text(encoding="utf-8")), identity
    )

    assert config["update"]["check"]["mode"] == "module"
    assert config["update"]["payload"]["mode"] == "zip"
