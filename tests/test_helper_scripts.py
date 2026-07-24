"""Cover user-visible output from package migration helper scripts.

Checked-in legacy fixtures and temporary TOML files are real. Windows shortcut
inspection and subprocess boundaries are mocked where required. Exact helper
call graphs and internal normalization structures are out of scope.
"""

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
PKG_PY = SRC_ROOT / "pkg" / "pkg.py"
LEGACY_CONVERTER = SRC_ROOT / "pkg" / "legacy_to_pkg_toml.py"
SHORTCUT_IMPORTER = SRC_ROOT / "pkg" / "shortcuts_to_pkg_toml.py"
EXAMPLES_ROOT = ROOT / "tests" / "fixtures" / "legacy_examples"


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


def load_shortcut_importer_module():
    spec = importlib.util.spec_from_file_location(
        "shortcuts_to_pkg_toml_under_test", SHORTCUT_IMPORTER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class LegacyConverterTests(unittest.TestCase):
    def run_converter(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
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
        """Converter writes canonical toml that pkg accepts."""
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
                                "content": '@echo off\r\n"$AppPath\\App\\good.exe" %*\r\n',
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            pkg_toml = version_dir / "pkg.toml"
            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(pkg_toml)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(pkg_toml.exists())

            rendered = pkg_toml.read_text(encoding="utf-8")
            parsed = tomllib.loads(rendered)
            self.assertNotIn("[[main]]", rendered)
            self.assertEqual(parsed["localVersion"], 1)
            self.assertFalse(parsed["only_portable"])
            self.assertIn("[[environment]]", rendered)
            self.assertIn('Name = "GOODAPP_HOME"', rendered)
            self.assertIn('targetPath = "$App\\\\good.exe"', rendered)
            self.assertEqual(parsed["description"], "Good test package")
            self.assertEqual(parsed["homepage"], "https://example.invalid/goodapp")
            self.assertEqual(
                parsed["origin"]["url"], "https://example.invalid/goodapp.zip"
            )
            self.assertEqual(
                [entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"]
            )
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
            (version_dir / "App").mkdir()
            (version_dir / "App" / "good.exe").write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(
                    module, "update_current_junction_if_needed", return_value=True
                ):
                    with mock.patch.object(
                        module,
                        "install_components",
                        return_value=mock.Mock(
                            ok=True, changed=False, warnings=[], errors=[]
                        ),
                    ) as install_components_mock:
                        code, output = self.run_main(module, [str(version_dir)])

            self.assertEqual(code, module.EXIT_SUCCESS, msg=output)
            install_components_mock.assert_called_once()

    def test_converter_can_infer_metadata_from_directory_name(self) -> None:
        """Converter can infer metadata from directory name."""
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

    def test_converter_moves_legacy_download_url_to_single_origin_table(self) -> None:
        """The converter emits a single origin table for legacy downloadURL metadata."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "OriginApp" / "v1.0.0.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.json").write_text(
                json.dumps({"downloadURL": "https://example.invalid/origin-app.zip"}),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertIn("[origin]", result.stdout)
            self.assertNotIn("[[origin]]", result.stdout)
            self.assertEqual(
                tomllib.loads(result.stdout)["origin"]["url"],
                "https://example.invalid/origin-app.zip",
            )

    def test_converter_preserves_existing_origin_configuration(self) -> None:
        """The converter retains canonical origin settings when it rewrites pkg.toml."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "OriginApp" / "v1.0.0.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.toml").write_text(
                """
                name = "OriginApp"
                version = "1.0.0"

                [origin]
                url = "https://example.invalid/current.zip"
                checksum = "sha256:current"
                extractSubdir = "current"

                [[origin.versions]]
                version = "0.9.0"
                script = "scripts/populate-old.cmd"
                """.lstrip(),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            origin = tomllib.loads(result.stdout)["origin"]
            self.assertEqual(origin["url"], "https://example.invalid/current.zip")
            self.assertEqual(origin["checksum"], "sha256:current")
            self.assertEqual(origin["extractSubdir"], "current")
            self.assertEqual(
                origin["versions"],
                [{"version": "0.9.0", "script": "scripts/populate-old.cmd"}],
            )

    def test_converter_preserves_existing_backups_before_replacing_output(self) -> None:
        """The converter creates a numbered backup without replacing an older backup."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "BackupApp" / "v1.0.0.l1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.json").write_text(
                json.dumps({"name": "BackupApp", "version": "1.0.0"}),
                encoding="utf-8",
            )
            pkg_toml = Path(tmpdir) / "pkg.toml"
            original = '# Retain this migration result.\nname = "Previous App"\n'
            pkg_toml.write_text(original, encoding="utf-8")
            first_backup = Path(str(pkg_toml) + ".bak")
            first_backup.write_text("# Older backup.\n", encoding="utf-8")

            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(pkg_toml)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertEqual(
                first_backup.read_text(encoding="utf-8"), "# Older backup.\n"
            )
            self.assertEqual(
                Path(str(pkg_toml) + ".bak.1").read_text(encoding="utf-8"), original
            )
            self.assertEqual(
                tomllib.loads(pkg_toml.read_text(encoding="utf-8"))["name"], "BackupApp"
            )
            self.assertIn(f"Backed up {pkg_toml} to {pkg_toml}.bak.1", result.stdout)

    def test_converter_rewrites_legacy_pkg_toml_aliases_to_canonical_schema(
        self,
    ) -> None:
        """Converter rewrites legacy pkg toml aliases to canonical schema."""
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
            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(converted)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            parsed = tomllib.loads(converted.read_text(encoding="utf-8"))
            self.assertEqual(parsed["name"], "AliasApp")
            self.assertEqual(parsed["version"], "3.4.5")
            self.assertEqual(parsed["localVersion"], 2)
            self.assertTrue(parsed["only_portable"])
            self.assertEqual(parsed["description"], "Legacy alias config")
            self.assertEqual(parsed["homepage"], "https://example.invalid/alias")
            self.assertEqual(
                parsed["origin"]["url"], "https://example.invalid/alias.zip"
            )
            self.assertEqual(
                [entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"]
            )
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
        """Converter accepts list root legacy sidecar files."""
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
                            {
                                "name": "ListRoot",
                                "target_path": "$AppPath\\App\\listroot.exe",
                            },
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
        """Converter reads all checked in legacy examples."""
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
                result = self.run_converter(
                    "--dir", str(EXAMPLES_ROOT / example_name), "--dry-run"
                )
                self.assertEqual(
                    result.returncode, 0, msg=result.stderr or result.stdout
                )

                parsed = tomllib.loads(result.stdout)
                self.assertEqual(parsed["name"], expected["name"])
                self.assertEqual(parsed["version"], expected["version"])
                self.assertEqual(parsed["localVersion"], expected["localVersion"])

                if "only_portable" in expected:
                    self.assertEqual(parsed["only_portable"], expected["only_portable"])
                if "shortcut_count" in expected:
                    self.assertEqual(
                        len(parsed.get("shortcut", [])), expected["shortcut_count"]
                    )
                if "bin_count" in expected:
                    self.assertEqual(len(parsed.get("bin", [])), expected["bin_count"])
                if "path_values" in expected:
                    self.assertEqual(
                        [entry["value"] for entry in parsed.get("path", [])],
                        expected["path_values"],
                    )
                if "shortcut_name" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["name"], expected["shortcut_name"]
                    )
                if "first_shortcut" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["name"], expected["first_shortcut"]
                    )
                if "shortcut_target" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["targetPath"], expected["shortcut_target"]
                    )


class ShortcutImporterTests(unittest.TestCase):
    def test_shortcut_reader_passes_the_lnk_path_through_stdin_json(self) -> None:
        """The reader passes shortcut paths with spaces through stdin JSON."""

        importer = load_shortcut_importer_module()
        shortcut_path = Path(
            r"C:\games\CommanderKeen\v1.4_42493.l1\_shortcuts\Commander Keen.lnk"
        )
        payload = {
            "TargetPath": r"C:\games\CommanderKeen\v1.4_42493.l1\App\keen.exe",
            "Arguments": "",
            "WorkingDirectory": "",
            "IconLocation": "",
            "Description": "",
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        with mock.patch.object(importer.os, "name", "nt"):
            with mock.patch.object(
                importer.subprocess, "run", return_value=completed
            ) as run_mock:
                self.assertEqual(importer.read_windows_shortcut(shortcut_path), payload)

        command = run_mock.call_args.args[0]
        self.assertIn("[Console]::In.ReadToEnd() | ConvertFrom-Json", command[5])
        self.assertIn(
            "$shell.CreateShortcut([string]$inputObject.ShortcutPath)", command[5]
        )
        self.assertNotIn("$args[0]", command[5])
        self.assertEqual(
            json.loads(run_mock.call_args.kwargs["input"]),
            {"ShortcutPath": str(shortcut_path)},
        )

    def test_importer_overwrites_matching_shortcuts_and_preserves_other_config(
        self,
    ) -> None:
        """The importer replaces same-name shortcut tables without regenerating unrelated TOML."""

        importer = load_shortcut_importer_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Game" / "v1.0.0.l1"
            shortcuts_dir = version_dir / "_shortcuts" / "Games"
            app_dir = version_dir / "App"
            icons_dir = version_dir / "Icons"
            shortcuts_dir.mkdir(parents=True)
            app_dir.mkdir()
            icons_dir.mkdir()

            (shortcuts_dir / "Launch Game.lnk").write_text("", encoding="utf-8")
            (version_dir / "pkg.toml").write_text(
                r"""
name = "Game"
version = "1.0.0"
localVersion = 1

[[environment]]
Name = "GAME_HOME"
Value = "$App"

[[shortcut]]
name = "Games\\Launch Game"
targetPath = "$App\\old.exe"
description = "Old shortcut"

[[shortcut]]
name = "Tools\\Keep Me"
targetPath = "$App\\tools\\keep.exe"
""".lstrip(),
                encoding="utf-8",
            )

            shortcut_payload = {
                "TargetPath": str(app_dir / "game.exe"),
                "Arguments": "--fullscreen",
                "WorkingDirectory": str(app_dir),
                "IconLocation": f"{icons_dir / 'game.ico'},0",
                "Description": "Launch Game",
            }
            with mock.patch.object(
                importer, "read_windows_shortcut", return_value=shortcut_payload
            ):
                rendered, shortcuts = importer.import_shortcuts(version_dir)

            parsed = tomllib.loads(rendered)
            self.assertEqual(len(shortcuts), 1)
            self.assertEqual(parsed["environment"][0]["Name"], "GAME_HOME")
            self.assertEqual(len(parsed["shortcut"]), 2)

            imported = next(
                item
                for item in parsed["shortcut"]
                if item["name"] == "Games\\Launch Game"
            )
            self.assertEqual(imported["targetPath"], "$App\\game.exe")
            self.assertEqual(imported["arguments"], "--fullscreen")
            self.assertEqual(imported["workingDirectory"], "$App")
            self.assertEqual(imported["iconLocation"], "$Icons\\game.ico,0")
            self.assertEqual(imported["description"], "Launch Game")

            preserved = next(
                item for item in parsed["shortcut"] if item["name"] == "Tools\\Keep Me"
            )
            self.assertEqual(preserved["targetPath"], "$App\\tools\\keep.exe")
            self.assertNotIn("old.exe", rendered)

    def test_importer_appends_new_shortcuts_from_nested_shortcut_directory(
        self,
    ) -> None:
        """Nested files under ``_shortcuts`` become nested shortcut names in TOML."""

        importer = load_shortcut_importer_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Tool" / "v2.0.0.l3"
            nested_dir = version_dir / "_shortcuts" / "Tools"
            app_dir = version_dir / "App" / "bin"
            nested_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            (nested_dir / "Tool.lnk").write_text("", encoding="utf-8")
            (version_dir / "pkg.toml").write_text(
                """
name = "Tool"
version = "2.0.0"
localVersion = 3
""".lstrip(),
                encoding="utf-8",
            )

            shortcut_payload = {
                "TargetPath": str(app_dir / "tool.exe"),
                "Arguments": "",
                "WorkingDirectory": "",
                "IconLocation": "",
                "Description": "",
            }
            with mock.patch.object(
                importer, "read_windows_shortcut", return_value=shortcut_payload
            ):
                rendered, _ = importer.import_shortcuts(version_dir)

            parsed = tomllib.loads(rendered)
            self.assertEqual(parsed["shortcut"][0]["name"], "Tools\\Tool")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\bin\\tool.exe")


if __name__ == "__main__":
    unittest.main()
