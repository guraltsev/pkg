"""Cover System Informer's package-local release discovery and staging hooks.

The GitHub response boundary is mocked while tag parsing, asset selection,
candidate construction, and staged-file cleanup are real. Downloading and ZIP
extraction are outside this hook's scope.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = (
    ROOT
    / "pkgs"
    / "systeminformer"
    / "vbootstrap.l1"
    / "pkg.local"
    / "check_update.py"
)
INSTALL_STEP_PATH = (
    ROOT
    / "pkgs"
    / "systeminformer"
    / "vbootstrap.l1"
    / "pkg.local"
    / "remove_settings.py"
)


def load_package_module(path: Path, name: str):
    """Load one System Informer package-local module for testing."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def load_update_hook():
    """Load the System Informer package-local update hook for testing."""
    return load_package_module(HOOK_PATH, "systeminformer_update_hook")


class SystemInformerUpdateHookTests(unittest.TestCase):
    def test_release_tag_selects_binary_zip_with_three_part_filename(self) -> None:
        """A four-part release tag selects the binary ZIP's three-part name."""
        hook = load_update_hook()
        response = io.BytesIO(
            b'{"id": 123, "tag_name": "v3.2.25011.2103", "assets": ['
            b'{"name": "systeminformer-3.2.25011-release-bin.zip", '
            b'"state": "uploaded", "browser_download_url": "https://example.test/bin.zip"}]}'
        )

        with mock.patch.object(hook.urllib.request, "urlopen", return_value=response):
            candidate = hook.check_update({"current": {"version": "bootstrap"}})

        self.assertEqual(
            candidate,
            {
                "candidateId": "systeminformer:123",
                "version": "3.2.25011.2103",
                "url": "https://example.test/bin.zip",
                "fileName": "systeminformer-3.2.25011-release-bin.zip",
            },
        )

    def test_healthy_current_release_returns_no_candidate(self) -> None:
        """The hook skips a release whose full tag and payload are current."""
        hook = load_update_hook()
        response = io.BytesIO(
            b'{"id": 123, "tag_name": "v3.2.25011.2103", "assets": []}'
        )

        with mock.patch.object(hook.urllib.request, "urlopen", return_value=response):
            candidate = hook.check_update(
                {"current": {"version": "3.2.25011.2103", "appReady": True}}
            )

        self.assertIsNone(candidate)

    def test_install_step_removes_settings_from_every_supported_architecture(self) -> None:
        """The staged payload excludes generated settings files for all architectures."""
        step = load_package_module(INSTALL_STEP_PATH, "systeminformer_install_step")
        with tempfile.TemporaryDirectory() as tmpdir:
            app_path = Path(tmpdir) / "App"
            for architecture in ("arm64", "amd64", "i386"):
                settings = app_path / architecture / "SystemInformer.exe.settings.xml"
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text("generated", encoding="utf-8")

            step.install_step({"paths": {"stageApp": app_path}})

            for architecture in ("arm64", "amd64", "i386"):
                self.assertFalse(
                    (app_path / architecture / "SystemInformer.exe.settings.xml").exists()
                )
