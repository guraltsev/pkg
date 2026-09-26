"""Cover Python selection and Windows launcher forwarding.

Windows ``cmd`` execution is real while a temporary command shim replaces the
system Python command. Package behavior, Python downloads, and batch-file
implementation details beyond launched arguments and the working directory are
out of scope.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"


class WrapperScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_gupkg_launcher_uses_path_python_when_no_runtime_is_downloaded(self) -> None:
        """The portable launcher delegates to an available system Python first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            launcher = temporary_root / "gupkg.cmd"
            launcher.write_text(
                (SRC_ROOT / "gupkg.cmd").read_text(encoding="utf-8"),
                encoding="ascii",
            )
            python_directory = temporary_root / "system-python"
            python_directory.mkdir()
            log_file = temporary_root / "system-python.log"
            (python_directory / "python.cmd").write_text(
                f'''@echo off
>> "{log_file}" echo args=%*
exit /b 0
''',
                encoding="ascii",
            )

            env = os.environ.copy()
            env.pop("GUPKG_PYTHON", None)
            env["PATH"] = f"{python_directory}{os.pathsep}{env.get('PATH', '')}"
            result = subprocess.run(
                ["cmd", "/c", str(launcher), "--probe"],
                cwd=str(temporary_root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded_calls = log_file.read_text(encoding="utf-8").splitlines()
            self.assertIn("-m gupkg --root", recorded_calls[-1])
            self.assertIn("--probe", recorded_calls[-1])

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
            self.assertIn("-m gupkg --root", recorded_args)
            self.assertIn(str(SRC_ROOT.parent), recorded_args)
            self.assertIn("tui --probe", recorded_args)

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_gupkg_wrapper_uses_its_own_directory_and_forwards_arguments(self) -> None:
        """The repository launcher resolves its module from its own directory."""
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
                ["cmd", "/c", str(SRC_ROOT / "gupkg.cmd"), "upgrade", "check"],
                cwd=str(version_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            lines = log_file.read_text(encoding="utf-8").splitlines()
            recorded_cwd = lines[0].removeprefix("cwd=")
            recorded_args = lines[1].removeprefix("args=")
            self.assertEqual(Path(recorded_cwd), SRC_ROOT)
            self.assertIn("-m gupkg --root", recorded_args)
            self.assertIn(str(SRC_ROOT.parent), recorded_args)
            self.assertIn("upgrade check", recorded_args)


if __name__ == "__main__":
    unittest.main()
