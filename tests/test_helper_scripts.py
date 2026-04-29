from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_PY = ROOT / "pkg.py"
LEGACY_CONVERTER = ROOT / "helper_scripts" / "legacy_to_pkg_toml.py"


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
            self.assertNotIn("[[main]]", rendered)
            self.assertIn("localVersion = 1", rendered)
            self.assertIn("only_portable = false", rendered)
            self.assertIn("[[environment]]", rendered)
            self.assertIn("Name = \"GOODAPP_HOME\"", rendered)
            self.assertIn("targetPath = \"$App\\\\good.exe\"", rendered)

            loaded = module.read_toml_file(pkg_toml)
            identity, _ = module.resolve_input_path(version_dir)
            config = module.normalize_runtime_config(loaded, identity)
            module.validate_runtime_config(config)

            self.assertEqual(config["description"], "Good test package")
            self.assertEqual(config["homepage"], "https://example.invalid/goodapp")
            self.assertEqual(config["downloadURL"], "https://example.invalid/goodapp.zip")
            self.assertEqual(config["path"], ["$App", "$App\\Tools"])
            self.assertEqual(config["environment"][0]["Name"], "GOODAPP_HOME")
            self.assertEqual(config["environment"][0]["Value"], "$App")
            self.assertEqual(config["shortcut"][0]["name"], "Good App")
            self.assertEqual(config["shortcut"][0]["targetPath"], "$App\\good.exe")
            self.assertEqual(config["shortcut"][0]["workingDirectory"], "$App")
            self.assertEqual(config["bin"][0]["name"], "good.cmd")
            self.assertIn("$App\\good.exe", config["bin"][0]["content"])

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


if __name__ == "__main__":
    unittest.main()
