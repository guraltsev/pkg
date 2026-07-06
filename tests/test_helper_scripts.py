from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
PKG_PY = SRC_ROOT / "pkg.py"
LEGACY_CONVERTER = SRC_ROOT / "helper_scripts" / "legacy_to_pkg_toml.py"
EXAMPLES_ROOT = SRC_ROOT / "helper_scripts" / "examples"


def load_pkg_module():
    spec = importlib.util.spec_from_file_location("pkg_under_test_helper", PKG_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class LegacyConverterTests(unittest.TestCase):
    def run_converter(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LEGACY_CONVERTER), *args],
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_main(self, module, args: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            try:
                code = module.main(args)
            except SystemExit as exc:
                code = exc.code
        if code is None:
            code = 0
        self.assertIsInstance(code, int)
        return code, stdout.getvalue()

    def test_converter_writes_canonical_toml_that_pkg_accepts(self) -> None:
        module = load_pkg_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "GoodApp" / "v1.2.3.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "opt_pkg.json").write_text(
                json.dumps(
                    {
                        "name": "GoodApp",
                        "version": "1.2.3",
                        "local_version": "1",
                        "portable": False,
                        "description": "Good test package",
                        "homepage": "https://example.invalid/goodapp",
                        "download_url": "https://example.invalid/goodapp.zip",
                        "path": ["$AppPath\\App", "$AppPath\\Tools"],
                        "env": [
                            {"name": "GOODAPP_HOME", "value": "$AppPath\\App"},
                        ],
                        "shortcut": [
                            {
                                "name": "Good App.lnk",
                                "target_path": "$AppPath\\App\\good.exe",
                                "working_directory": "$AppPath\\App",
                            }
                        ],
                        "bin": [
                            {
                                "name": "good.cmd",
                                "content": "@echo off\r\n\"$AppPath\\App\\good.exe\" %*\r\n",
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            pkg_toml = version_dir / "pkg.toml"
            result = self.run_converter("--dir", str(version_dir), "--output", str(pkg_toml))

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(pkg_toml.exists())

            rendered = pkg_toml.read_text(encoding="utf-8")
            parsed = tomllib.loads(rendered)
            self.assertNotIn("[[main]]", rendered)
            self.assertEqual(parsed["localVersion"], 1)
            self.assertFalse(parsed["only_portable"])
            self.assertIn("[[environment]]", rendered)
            self.assertIn("Name = \"GOODAPP_HOME\"", rendered)
            self.assertIn("targetPath = \"$App\\\\good.exe\"", rendered)
            self.assertEqual(parsed["description"], "Good test package")
            self.assertEqual(parsed["homepage"], "https://example.invalid/goodapp")
            self.assertEqual(parsed["downloadURL"], "https://example.invalid/goodapp.zip")
            self.assertEqual([entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"])
            self.assertEqual(parsed["environment"][0]["Name"], "GOODAPP_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["name"], "Good App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\good.exe")
            self.assertEqual(parsed["shortcut"][0]["workingDirectory"], "$App")
            self.assertEqual(parsed["bin"][0]["name"], "good.cmd")
            self.assertIn("$App\\good.exe", parsed["bin"][0]["content"])

            env = {
                "APPDATA": str(Path(tmpdir) / "AppData"),
                "USERPROFILE": str(Path(tmpdir) / "UserProfile"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(module, "update_current_junction_if_needed", return_value=True):
                    with mock.patch.object(
                        module,
                        "install_components",
                        return_value=module.StepResult(ok=True, changed=False),
                    ) as install_components_mock:
                        code, output = self.run_main(module, [str(version_dir)])

            self.assertEqual(code, module.EXIT_SUCCESS, msg=output)
            install_components_mock.assert_called_once()

    def test_converter_can_infer_metadata_from_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Tool-portable" / "v2.0.1.l7"
            version_dir.mkdir(parents=True)
            (version_dir / "shortcut.json").write_text(
                json.dumps(
                    {
                        "shortcut": [
                            {
                                "name": "Tool",
                                "targetPath": "$AppPath\\App\\tool.exe",
                            }
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

            rendered = result.stdout
            parsed = tomllib.loads(rendered)
            self.assertEqual(parsed["name"], "Tool-portable")
            self.assertEqual(parsed["version"], "2.0.1")
            self.assertEqual(parsed["localVersion"], 7)
            self.assertTrue(parsed["only_portable"])
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\tool.exe")

    def test_converter_rewrites_legacy_pkg_toml_aliases_to_canonical_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "AliasApp" / "v3.4.5.l2"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.toml").write_text(
                r"""
                [[main]]
                name = "AliasApp"
                version = "3.4.5"
                local_version = 2
                portable = true
                description = "Legacy alias config"
                homepage = "https://example.invalid/alias"
                download_url = "https://example.invalid/alias.zip"

                env = [{ name = "ALIASAPP_HOME", value = '$AppPath\App' }]
                shortcuts = [
                  { name = "Alias App.lnk", path = '$AppPath\App\alias.exe', args = "--legacy", workdir = '$AppPath\App', icon_location = '$AppPath\Icons\alias.ico', desc = "Legacy shortcut" }
                ]
                path = ['$AppPath\App', '$AppPath\Tools']
                bin = [{ name = "alias.cmd", content = '''@echo off
                "$AppPath\App\alias.exe" %*
                ''' }]
                """,
                encoding="utf-8",
            )

            converted = version_dir / "converted.toml"
            result = self.run_converter("--dir", str(version_dir), "--output", str(converted))

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            parsed = tomllib.loads(converted.read_text(encoding="utf-8"))
            self.assertEqual(parsed["name"], "AliasApp")
            self.assertEqual(parsed["version"], "3.4.5")
            self.assertEqual(parsed["localVersion"], 2)
            self.assertTrue(parsed["only_portable"])
            self.assertEqual(parsed["description"], "Legacy alias config")
            self.assertEqual(parsed["homepage"], "https://example.invalid/alias")
            self.assertEqual(parsed["downloadURL"], "https://example.invalid/alias.zip")
            self.assertEqual([entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"])
            self.assertEqual(parsed["environment"][0]["Name"], "ALIASAPP_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["name"], "Alias App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\alias.exe")
            self.assertEqual(parsed["shortcut"][0]["arguments"], "--legacy")
            self.assertEqual(parsed["shortcut"][0]["workingDirectory"], "$App")
            self.assertEqual(parsed["shortcut"][0]["iconLocation"], "$Icons\\alias.ico")
            self.assertEqual(parsed["shortcut"][0]["description"], "Legacy shortcut")
            self.assertIn("$App\\alias.exe", parsed["bin"][0]["content"])

    def test_converter_accepts_list_root_legacy_sidecar_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "ListRootApp" / "v1.0.0.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "environment.json").write_text(
                json.dumps(
                    [
                        {"name": "LISTROOT_HOME", "value": "$AppPath\\App"},
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            (version_dir / "shortcuts.json").write_text(
                json.dumps(
                    {
                        "shortcuts": [
                            {"name": "ListRoot", "target_path": "$AppPath\\App\\listroot.exe"},
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            parsed = tomllib.loads(result.stdout)
            self.assertEqual(parsed["environment"][0]["Name"], "LISTROOT_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\listroot.exe")

    def test_converter_reads_all_checked_in_legacy_examples(self) -> None:
        cases = [
            (
                "1",
                {
                    "name": "WUMgr",
                    "version": "1.1b",
                    "localVersion": 1,
                    "shortcut_count": 1,
                    "shortcut_name": "Tools\\WU Mgr",
                    "shortcut_target": "$App\\WUMgr.exe",
                },
            ),
            (
                "2",
                {
                    "name": "emacs",
                    "version": "30.2",
                    "localVersion": 2,
                    "only_portable": False,
                    "shortcut_count": 3,
                    "bin_count": 3,
                    "first_shortcut": "Emacs",
                },
            ),
            (
                "3",
                {
                    "name": "PortableGit",
                    "version": "2.46.2",
                    "localVersion": 4,
                    "shortcut_count": 1,
                    "path_values": ["$App\\cmd"],
                    "shortcut_target": "$App\\git-bash.exe",
                },
            ),
            (
                "4",
                {
                    "name": "Tixati",
                    "version": "3.29-1",
                    "localVersion": 6,
                    "shortcut_count": 1,
                    "shortcut_target": "$App\\tixati_Windows64bit.exe",
                },
            ),
            (
                "5",
                {
                    "name": "ISLANDERS - New Shores",
                    "version": "0",
                    "localVersion": 1,
                    "only_portable": False,
                    "shortcut_count": 1,
                    "shortcut_name": "Games\\Islanders - New Shores",
                    "shortcut_target": "$App\\Islanders New Shores.exe",
                },
            ),
        ]

        for example_name, expected in cases:
            with self.subTest(example=example_name):
                result = self.run_converter("--dir", str(EXAMPLES_ROOT / example_name), "--dry-run")
                self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

                parsed = tomllib.loads(result.stdout)
                self.assertEqual(parsed["name"], expected["name"])
                self.assertEqual(parsed["version"], expected["version"])
                self.assertEqual(parsed["localVersion"], expected["localVersion"])

                if "only_portable" in expected:
                    self.assertEqual(parsed["only_portable"], expected["only_portable"])
                if "shortcut_count" in expected:
                    self.assertEqual(len(parsed.get("shortcut", [])), expected["shortcut_count"])
                if "bin_count" in expected:
                    self.assertEqual(len(parsed.get("bin", [])), expected["bin_count"])
                if "path_values" in expected:
                    self.assertEqual([entry["value"] for entry in parsed.get("path", [])], expected["path_values"])
                if "shortcut_name" in expected:
                    self.assertEqual(parsed["shortcut"][0]["name"], expected["shortcut_name"])
                if "first_shortcut" in expected:
                    self.assertEqual(parsed["shortcut"][0]["name"], expected["first_shortcut"])
                if "shortcut_target" in expected:
                    self.assertEqual(parsed["shortcut"][0]["targetPath"], expected["shortcut_target"])


if __name__ == "__main__":
    unittest.main()
