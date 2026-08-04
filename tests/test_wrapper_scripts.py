"""Cover Windows wrapper forwarding and helper-script target resolution.

Windows ``cmd`` execution is real while temporary command shims replace the
selected Python interpreter. Package installation behavior and batch-file
implementation details beyond forwarded paths and arguments are out of scope.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
WRAPPER_SCRIPTS = [
    (SRC_ROOT / "install.cmd", ["--pause", "install"]),
    (SRC_ROOT / "update-config.cmd", ["--pause", "config", "update"]),
    (SRC_ROOT / "health-check.cmd", ["--pause", "config", "check"]),
    (SRC_ROOT / "check-update.cmd", ["--pause", "upgrade", "check"]),
    (SRC_ROOT / "update.cmd", ["--pause", "upgrade", "download"]),
    (SRC_ROOT / "refresh-app.cmd", ["--refresh-app", "--pause", "install"]),
    (
        SRC_ROOT / "legacy_to_gupkg_toml.cmd",
        ["config", "from-legacy"],
    ),
]
HELPER_WRAPPER_SCRIPTS = [
    (
        SRC_ROOT / "shortcuts_to_gupkg_toml.cmd",
        SRC_ROOT / "gupkg" / "shortcuts_to_gupkg_toml.py",
    ),
]


class WrapperScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_gupkg_tui_wrapper_opens_the_interactive_command(self) -> None:
        """The TUI wrapper forwards its arguments to ``gupkg tui``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "tui-wrapper.log"
            fake_python = Path(tmpdir) / "fake-python.cmd"
            fake_python.write_text(
                f'''@echo off
> "{log_file}" echo args=%*
exit /b 0
''',
                encoding="ascii",
            )

            env = os.environ.copy()
            env["GUPKG_PYTHON"] = str(fake_python)
            result = subprocess.run(
                ["cmd", "/c", str(SRC_ROOT / "gupkg-tui.cmd"), "--probe"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded_args = (
                log_file.read_text(encoding="utf-8").removeprefix("args=").strip()
            )
            self.assertIn("-m gupkg tui --probe", recorded_args)

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_wrapper_scripts_preserve_caller_working_directory(self) -> None:
        """Wrapper scripts preserve caller working directory."""
        for script, forwarded_args in WRAPPER_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    version_dir = Path(tmpdir) / "WrapperApp" / "v1.0.0.l1"
                    version_dir.mkdir(parents=True)
                    log_file = Path(tmpdir) / "wrapper.log"
                    fake_python = Path(tmpdir) / "fake-python.cmd"
                    fake_python.write_text(
                        f"""@echo off
> "{log_file}" echo cwd=%CD%
>> "{log_file}" echo args=%*
exit /b 0
""",
                        encoding="ascii",
                    )

                    env = os.environ.copy()
                    env["GUPKG_PYTHON"] = str(fake_python)
                    result = subprocess.run(
                        ["cmd", "/c", str(script)],
                        cwd=str(version_dir),
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(
                        result.returncode, 0, msg=result.stderr or result.stdout
                    )
                    lines = log_file.read_text(encoding="utf-8").splitlines()
                    recorded_cwd = lines[0].removeprefix("cwd=")
                    recorded_args = lines[1].removeprefix("args=")
                    self.assertEqual(Path(recorded_cwd), version_dir)
                    self.assertIn(str(SRC_ROOT / "gupkg" / "gupkg.py"), recorded_args)
                    for arg in forwarded_args:
                        self.assertIn(arg, recorded_args)

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_helper_wrappers_resolve_python_scripts_in_helper_subdir(self) -> None:
        """Helper wrappers resolve python scripts in helper subdir."""
        for script, expected_target in HELPER_WRAPPER_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    log_file = Path(tmpdir) / "helper-wrapper.log"
                    fake_py = Path(tmpdir) / "py.cmd"
                    fake_python = Path(tmpdir) / "python.cmd"
                    shim_content = f"""@echo off
> "{log_file}" echo cwd=%CD%
>> "{log_file}" echo args=%*
exit /b 0
"""
                    fake_py.write_text(shim_content, encoding="ascii")
                    fake_python.write_text(shim_content, encoding="ascii")

                    env = os.environ.copy()
                    env["PATH"] = f"{tmpdir}{os.pathsep}{env.get('PATH', '')}"
                    result = subprocess.run(
                        ["cmd", "/c", str(script), "--probe"],
                        cwd=str(ROOT),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(
                        result.returncode, 0, msg=result.stderr or result.stdout
                    )
                    lines = log_file.read_text(encoding="utf-8").splitlines()
                    recorded_args = lines[1].removeprefix("args=")
                    self.assertIn(str(expected_target), recorded_args)
                    self.assertIn("--probe", recorded_args)


if __name__ == "__main__":
    unittest.main()
