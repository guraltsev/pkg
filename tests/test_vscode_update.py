"""Cover VS Code stable-release discovery through its package-local hook.

The HTTP response boundary is mocked while JSON parsing, candidate selection,
and URL construction are real. Package-manager download and ZIP extraction are
covered by the broader CLI behavior suite and are out of scope here.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import tomllib
import unittest
from unittest import mock

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "pkgs" / "vscode" / "vbootstrap.l1" / "pkg.toml"
HOOK_PATH = (
    ROOT
    / "pkgs"
    / "vscode"
    / "vbootstrap.l1"
    / "pkg.local"
    / "check_update.py"
)


def load_update_hook():
    """Load the VS Code package-local update hook for behavior testing."""
    spec = importlib.util.spec_from_file_location("vscode_update_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_bootstrap_manifest_installs_the_gui_code_command_shim() -> None:
    """The VS Code package exposes ``code.exe`` as a GUI launcher."""
    version_path = MANIFEST.parent
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )

    config = normalize_runtime_config(
        tomllib.loads(MANIFEST.read_text(encoding="utf-8")), identity
    )

    assert config["bin"][0]["name"] == "code"
    assert config["bin"][0]["target"] == "$App\\Code.exe"
    assert config["bin"][0]["type"] == "gui"


class VSCodeUpdateHookTests(unittest.TestCase):
    def test_latest_stable_release_builds_windows_archive_candidate(self) -> None:
        """The first stable API version selects its Windows x64 archive."""
        hook = load_update_hook()
        response = io.BytesIO(b'["1.130.0", "1.129.1"]')
        context = {
            "channel": "stable",
            "current": {"version": "1.129.1"},
        }

        with mock.patch.object(
            hook.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            candidate = hook.check_update(context)

        self.assertEqual(
            candidate,
            {
                "candidateId": "vscode-stable:1.130.0",
                "version": "1.130.0",
                "url": (
                    "https://update.code.visualstudio.com/"
                    "1.130.0/win32-x64-archive/stable"
                ),
                "fileName": "VSCode-win32-x64-1.130.0.zip",
            },
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, hook.RELEASES_URL)

    def test_current_stable_release_returns_no_candidate(self) -> None:
        """The hook reports no update when the installed version is newest."""
        hook = load_update_hook()
        response = io.BytesIO(b'["1.130.0", "1.129.1"]')
        context = {
            "channel": "stable",
            "current": {"version": "1.130.0"},
        }

        with mock.patch.object(
            hook.urllib.request, "urlopen", return_value=response
        ):
            candidate = hook.check_update(context)

        self.assertIsNone(candidate)


if __name__ == "__main__":
    unittest.main()
