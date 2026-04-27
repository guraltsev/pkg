from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_PY = ROOT / "pkg.py"
WRAPPER_SCRIPTS = [
    ROOT / "install.cmd",
    ROOT / "install-machine.cmd",
    ROOT / "update-config.cmd",
]
REMOVED_MODULES = [
    ROOT / "pkg_common.py",
    ROOT / "pkg_core.py",
    ROOT / "pkg_windows.py",
]
SECTION_MARKERS = {
    "shared": "# Section: Shared models and pure helpers",
    "windows": "# Section: Windows integration boundary",
    "core": "# Section: Package-management logic and CLI",
    "entry": "# Section: Script entry point",
}


class WrapperScriptTests(unittest.TestCase):
    def test_wrapper_scripts_preserve_caller_working_directory(self) -> None:
        """Ensure convenience wrappers do not change away from the caller's directory."""
        for script in WRAPPER_SCRIPTS:
            script_text = script.read_text(encoding="utf-8")
            self.assertNotIn(
                "cd /d",
                script_text.lower(),
                msg=f"Wrapper unexpectedly changes directory: {script.name}",
            )


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_single_file_layout_removes_old_implementation_modules(self) -> None:
        """Ensure the old split implementation files are no longer present."""
        for path in REMOVED_MODULES:
            self.assertFalse(path.exists(), msg=f"Unexpected legacy module present: {path.name}")

    def test_pkg_py_contains_required_section_markers(self) -> None:
        """Ensure ``pkg.py`` exposes the expected architecture sections."""
        source = PKG_PY.read_text(encoding="utf-8")
        for marker in SECTION_MARKERS.values():
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
