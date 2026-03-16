from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_PY = ROOT / "pkg.py"


def load_pkg_module():
    spec = importlib.util.spec_from_file_location("pkg_under_test", PKG_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PkgPureImportTests(unittest.TestCase):
    def test_import_succeeds(self) -> None:
        module = load_pkg_module()
        self.assertEqual(module.__version__, "0.10.0")

    def test_version_helpers_are_callable(self) -> None:
        module = load_pkg_module()
        self.assertTrue(module.is_version_directory_name("v1.2.3.l4"))
        self.assertEqual(module.compare_package_versions("v2.0.0.l1", "v1.9.9.l9"), 1)


if __name__ == "__main__":
    unittest.main()
