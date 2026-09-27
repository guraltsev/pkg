"""Cover containment of generated embedded-runtime support files.

The test uses a temporary package directory and substitutes the command
dispatcher and pip bootstrap. Downloading packages and executing a real
embedded interpreter are outside this test's scope.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.runtime_paths import find_runtime_directory

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRECTORY = find_runtime_directory(ROOT)
BOOTSTRAP = RUNTIME_DIRECTORY / "gupkg" / "bootstrap.py"
DEPENDENCIES = RUNTIME_DIRECTORY / "gupkg" / "dependencies.py"


def load_bootstrap_module() -> types.ModuleType:
    """Load bootstrap under an isolated name for each containment test."""
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_dependencies_module() -> types.ModuleType:
    """Load the dependency installer under an isolated name for each test."""
    spec = importlib.util.spec_from_file_location("dependencies_under_test", DEPENDENCIES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmbeddedBootstrapTests(unittest.TestCase):
    def test_embedded_mode_writes_only_to_the_adjacent_bundle(self) -> None:
        """Embedded mode creates support files only below bootstrap's local python directory."""
        module = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            package_directory = Path(tmpdir) / "gupkg"
            package_directory.mkdir()
            bootstrap_path = package_directory / "bootstrap.py"
            bootstrap_path.touch()
            (package_directory / "python").mkdir()
            external_directory = Path(tmpdir) / "external-python"
            external_directory.mkdir()
            package = types.ModuleType("gupkg")
            package.__path__ = []
            dispatcher = types.ModuleType("gupkg.gupkg")
            dispatcher.main = lambda arguments: 0

            with (
                mock.patch.object(module, "__file__", str(bootstrap_path)),
                mock.patch.object(module, "_ensure_pip"),
                mock.patch.dict(
                    sys.modules,
                    {"gupkg": package, "gupkg.gupkg": dispatcher},
                ),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                self.assertEqual(module.main(["--embedded"]), 0)

            pth_name = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
            self.assertTrue((package_directory / "python" / pth_name).is_file())
            self.assertFalse((external_directory / pth_name).exists())
            self.assertFalse((external_directory / "sitecustomize.py").exists())

    def test_bundled_dependencies_target_the_adjacent_runtime_directory(self) -> None:
        """Bundled dependency installs target only the adjacent local site-packages directory."""
        module = load_dependencies_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            package_directory = Path(tmpdir) / "gupkg"
            package_directory.mkdir()
            dependencies_path = package_directory / "dependencies.py"
            dependencies_path.touch()
            expected_target = package_directory / "python" / "Lib" / "site-packages"

            with (
                mock.patch.object(module, "__file__", str(dependencies_path)),
                mock.patch.dict(
                    os.environ, {"GUPKG_BUNDLED_RUNTIME": "1"}, clear=False
                ),
                mock.patch.object(module, "_module_is_importable", side_effect=[False, True]),
                mock.patch.object(module.site, "addsitedir"),
                mock.patch.object(
                    module.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)
                ) as run,
            ):
                module.install_missing_dependency("example_dependency")

            self.assertTrue(expected_target.is_dir())
            command = run.call_args.args[0]
            actual_target = Path(command[command.index("--target") + 1])
            self.assertEqual(actual_target.resolve(), expected_target.resolve())

    def test_system_python_dependencies_target_the_isolated_user_directory(self) -> None:
        """System Python installs gupkg dependencies below the isolated user directory."""
        module = load_dependencies_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            local_app_data = Path(tmpdir) / "local-app-data"
            expected_target = (
                local_app_data / "gupkg" / "embedded" / "site-packages"
            )

            with (
                mock.patch.dict(
                    os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=True
                ),
                mock.patch.object(module, "_module_is_importable", side_effect=[False, True]),
                mock.patch.object(module.site, "addsitedir"),
                mock.patch.object(
                    module.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)
                ) as run,
            ):
                module.install_missing_dependency("example_dependency")

            self.assertTrue(expected_target.is_dir())
            command = run.call_args.args[0]
            actual_target = Path(command[command.index("--target") + 1])
            self.assertEqual(actual_target.resolve(), expected_target.resolve())
