"""Cover standalone command information outside a package directory.

The real module entry point runs in a temporary non-package directory through
the active Python interpreter.  No package or manager data, network activity,
or Windows integration is involved.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRECTORY = ROOT / "src" / "v0.1.l1"
RELEASE_VERSION = "0.1"


class RuntimeCommandTests(unittest.TestCase):
    """Verify command information that does not require a package selection."""

    def run_command(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run the real module command without allowing bytecode writes to the source tree."""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(RUNTIME_DIRECTORY)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-m", "gupkg", *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_version_matches_the_declared_release_and_runtime_directory(self) -> None:
        """The declared release, runtime directory, and version command agree exactly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.run_command("--version", cwd=Path(tmpdir))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        directory_identity = re.fullmatch(r"v(.+)\.l\d+", RUNTIME_DIRECTORY.name)
        self.assertIsNotNone(directory_identity)
        self.assertEqual(directory_identity.group(1), RELEASE_VERSION)
        self.assertTrue(
            result.stdout.strip().endswith(f" {RELEASE_VERSION}"),
            msg=result.stdout,
        )

    def test_help_exits_without_selecting_a_package(self) -> None:
        """The help command succeeds from a directory that contains no package."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.run_command("--help", cwd=Path(tmpdir))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Local Package Manager for Windows", result.stdout)

    def test_config_check_accepts_a_version_directory_without_a_local_revision(self) -> None:
        """A version directory without ``.lN`` has local revision zero for metadata checks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "Example"
            version_directory = package_root / "v1.2.3"
            app_directory = version_directory / "App"
            app_directory.mkdir(parents=True)
            (app_directory / "payload.txt").write_text("payload", encoding="utf-8")
            (version_directory / "pkg.toml").write_text(
                'name = "Example"\nversion = "1.2.3"\nlocalVersion = 0\n',
                encoding="utf-8",
            )
            result = self.run_command(
                "config", "check", str(version_directory), cwd=package_root
            )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_legacy_converter_infers_zero_for_an_absent_local_revision(self) -> None:
        """Legacy conversion writes local revision zero for a directory without ``.lN``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_directory = Path(tmpdir) / "Example" / "v1.2.3"
            version_directory.mkdir(parents=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RUNTIME_DIRECTORY / "gupkg" / "legacy_to_gupkg_toml.py"),
                    "--dir",
                    str(version_directory),
                    "--dry-run",
                ],
                cwd=version_directory,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("localVersion = 0", result.stdout)
