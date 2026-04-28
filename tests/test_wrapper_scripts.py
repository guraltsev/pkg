from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER_SCRIPTS = [
    ROOT / "install.cmd",
    ROOT / "install-machine.cmd",
    ROOT / "update-config.cmd",
]


class WrapperScriptTests(unittest.TestCase):
    def test_wrapper_scripts_preserve_caller_working_directory(self) -> None:
        """Wrapper scripts should leave path resolution relative to the caller."""

        for script in WRAPPER_SCRIPTS:
            script_text = script.read_text(encoding="utf-8").lower()
            self.assertNotIn("cd /d", script_text, msg=f"Wrapper unexpectedly changes directory: {script.name}")


if __name__ == "__main__":
    unittest.main()
