from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG_PY = ROOT / "pkg.py"
FIXTURES = ROOT / "tests" / "fixtures"


def load_pkg_module():
    spec = importlib.util.spec_from_file_location("pkg_under_test", PKG_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class PkgPureImportTests(unittest.TestCase):
    def test_import_succeeds(self) -> None:
        module = load_pkg_module()
        self.assertEqual(module.__version__, "0.11.0")

    def test_version_helpers_are_callable(self) -> None:
        module = load_pkg_module()
        self.assertTrue(module.is_version_directory_name("v1.2.3.l4"))
        self.assertEqual(module.compare_package_versions("v2.0.0.l1", "v1.9.9.l9"), 1)

    def test_machine_scope_manager_init_does_not_exit(self) -> None:
        module = load_pkg_module()
        manager = module.PackageManager(scope=module.Scope.MACHINE)
        self.assertEqual(manager.scope, module.Scope.MACHINE)

    def test_invalid_install_path_returns_nonzero(self) -> None:
        module = load_pkg_module()
        manager = module.PackageManager()
        with contextlib.redirect_stdout(io.StringIO()):
            result = manager.install(ROOT / "does-not-exist")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, module.EXIT_USER_ERROR)

    def test_invalid_config_returns_nonzero(self) -> None:
        module = load_pkg_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "BrokenApp" / "v1.0.0.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.toml").write_text(
                """name = "BrokenApp"
version = "1.0.0"
localVersion = 1

[[shortcut]]
name = "Broken"
""",
                encoding="utf-8",
            )
            manager = module.PackageManager()
            with mock.patch.dict(os.environ, {"APPDATA": tmpdir, "USERPROFILE": tmpdir}, clear=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = manager.install(version_dir)
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, module.EXIT_USER_ERROR)

    def test_shortcut_creation_failure_returns_nonzero(self) -> None:
        module = load_pkg_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = FIXTURES / "GoodApp"
            dst = Path(tmpdir) / "GoodApp"
            shutil.copytree(src, dst)
            version_dir = dst / "v1.2.3.l1"
            manager = module.PackageManager()

            original_update_current = module.JunctionManager.update_current_junction_if_needed
            original_install_shortcuts = module.ShortcutInstaller.install_shortcuts
            original_install_environment = module.EnvironmentVariableManager.install_environment_variables
            original_ensure_bin = module.PATHManager.ensure_bin_in_path
            original_add_to_path = module.PATHManager.add_to_path
            original_install_wrappers = module.BinFileCreator.install_wrappers
            try:
                module.JunctionManager.update_current_junction_if_needed = staticmethod(lambda metadata, force=False: True)
                module.ShortcutInstaller.install_shortcuts = staticmethod(
                    lambda metadata, reporter=None: module.StepResult(ok=False, errors=["Failed to create shortcut: Good App"])
                )
                module.EnvironmentVariableManager.install_environment_variables = staticmethod(
                    lambda metadata, reporter=None: module.StepResult(ok=True, changed=False)
                )
                module.PATHManager.ensure_bin_in_path = staticmethod(
                    lambda metadata, reporter=None: module.StepResult(ok=True, changed=False)
                )
                module.PATHManager.add_to_path = staticmethod(
                    lambda new_entries, metadata, reporter=None: module.StepResult(ok=True, changed=False)
                )
                module.BinFileCreator.install_wrappers = staticmethod(
                    lambda metadata, reporter=None: module.StepResult(ok=True, changed=False)
                )
                with mock.patch.dict(os.environ, {"APPDATA": tmpdir, "USERPROFILE": tmpdir}, clear=False):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = manager.install(version_dir)
            finally:
                module.JunctionManager.update_current_junction_if_needed = original_update_current
                module.ShortcutInstaller.install_shortcuts = original_install_shortcuts
                module.EnvironmentVariableManager.install_environment_variables = original_install_environment
                module.PATHManager.ensure_bin_in_path = original_ensure_bin
                module.PATHManager.add_to_path = original_add_to_path
                module.BinFileCreator.install_wrappers = original_install_wrappers

            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, module.EXIT_MUTATION_ERROR)

    def test_missing_config_uses_defaults_without_writing(self) -> None:
        module = load_pkg_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = FIXTURES / "NoConfigApp"
            dst = Path(tmpdir) / "NoConfigApp"
            shutil.copytree(src, dst)
            version_dir = dst / "v0.9.0.l1"
            metadata = module.PackageMetadata(version_dir)
            config, warnings = metadata.load_config()
            self.assertEqual(config["name"], "NoConfigApp")
            self.assertTrue(warnings)
            self.assertFalse((version_dir / "pkg.toml").exists())

    def test_update_config_preserves_comments_unknown_keys_and_creates_backup(self) -> None:
        module = load_pkg_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = FIXTURES / "MismatchApp"
            dst = Path(tmpdir) / "MismatchApp"
            shutil.copytree(src, dst)
            version_dir = dst / "v2.0.0.l3"
            manager = module.PackageManager(scope=module.Scope.MACHINE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = manager.update_config(version_dir)
            self.assertTrue(result.ok)
            self.assertTrue(result.changed)

            pkg_toml = version_dir / "pkg.toml"
            updated = pkg_toml.read_text(encoding="utf-8")
            backup = (version_dir / "pkg.toml.bak").read_text(encoding="utf-8")

            self.assertIn('# Name is stale on purpose.', updated)
            self.assertIn('name = "MismatchApp"', updated)
            self.assertIn('version = "2.0.0"', updated)
            self.assertIn('localVersion = 3', updated)
            self.assertIn('only_portable = false', updated)
            self.assertIn('name = "MismatchApp-OLD"', backup)

    def test_resolve_input_path_classifies_version_dir_without_resolving_first(self) -> None:
        module = load_pkg_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "GoodApp" / "v1.2.3.l1"
            version_dir.mkdir(parents=True)
            resolved = module.resolve_input_path(version_dir)
            self.assertEqual(resolved.input_kind, "version")
            self.assertEqual(resolved.version_path, version_dir)
            self.assertFalse(resolved.installing_from_current)

    def test_alias_heavy_runtime_config_normalizes_to_runtime_model(self) -> None:
        module = load_pkg_module()
        identity = module.PackageIdentity(
            name="AliasApp",
            version="1.0.0",
            local_version=2,
            version_string="v1.0.0.l2",
            package_root=Path("C:/pkg/AliasApp"),
            version_path=Path("C:/pkg/AliasApp/v1.0.0.l2"),
            is_current=False,
            only_portable_by_name=False,
        )
        raw = {
            "NAME": "AliasApp",
            "Version": "1.0.0",
            "local_version": 2,
            "ENV": [{"name": "HOME", "value": "$App"}],
            "Shortcuts": [{"name": "Alias", "path": r"$App\Alias.exe"}],
            "PATH": [{"path": r"$App\bin"}],
            "BIN": [{"name": "alias.cmd", "content": "@echo off"}],
        }
        config = module.normalize_runtime_config(raw, identity)
        module.validate_runtime_config(config)
        self.assertEqual(config.environment[0].name, "HOME")
        self.assertEqual(config.shortcut[0].target_path, r"$App\Alias.exe")
        self.assertEqual(config.path, [r"$App\bin"])
        self.assertEqual(config.bin[0].name, "alias.cmd")


if __name__ == "__main__":
    unittest.main()
