"""Cover SumatraPDF Portable release discovery and executable path expansion.

The SumatraPDF download page boundary is mocked, while the package-local update
module and normal package-variable expansion are real. Downloading and ZIP
extraction are covered by the package manager's broader update tests and are
out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import tomllib
from pathlib import Path
from unittest import mock

from gupkg.configuration import normalize_runtime_config
from gupkg.core import ExpansionMode, PackageIdentity, expand_text


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


def test_version_variable_expands_to_the_active_package_version(tmp_path) -> None:
    """The wrapper executable name follows the immutable release directory version."""
    identity = PackageIdentity.from_version_path(
        tmp_path / "SumatraPDF-portable",
        tmp_path / "SumatraPDF-portable" / "v3.6.1.l1",
        is_current=False,
    )

    expansion = expand_text(
        "$App\\SumatraPDF-${version}-64.exe", identity, ExpansionMode.SCRIPT
    )

    assert expansion.value.endswith(r"current\App\SumatraPDF-3.6.1-64.exe")
    assert expansion.unresolved == []


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
