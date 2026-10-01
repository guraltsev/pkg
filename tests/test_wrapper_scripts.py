"""Cover observable launcher selection, forwarding, and bootstrap handoff.

The tests run real Windows ``cmd`` wrappers and the checked-in native shim;
the shim's target is a temporary Python probe. Package management, downloads,
and the native shim implementation itself are out of scope.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
# The dynamic launcher requires its MinGW runtime DLLs beside the executable.
# These isolated wrapper tests intentionally copy one file, so use the
# self-contained build and keep the packaging test responsible for dynamic
# dependency provisioning.
SHIM = SRC_ROOT / "gupkg" / "shim" / "shim-console.static.exe"


class WrapperScriptTests(unittest.TestCase):
    def make_probe(self, directory: Path, name: str, exit_code: int = 0) -> Path:
        """Create a Python child that records its cwd and forwarded arguments."""
        log_file = directory / f"{name}.log"
        probe = directory / f"{name}.py"
        probe.write_text(
            f"""from pathlib import Path
import sys

Path({log_file.as_posix()!r}).write_text(
    f\"cwd={{Path.cwd()}}\\nargs={{sys.argv[1:]!r}}\", encoding=\"utf-8\"
)
raise SystemExit({exit_code})
""",
            encoding="utf-8",
        )
        return log_file

    def make_executable(self, directory: Path, name: str, probe: Path) -> Path:
        """Create a native command shim targeting the temporary probe."""
        executable = directory / name
        shutil.copy2(SHIM, executable)
        target = sys.executable.replace("\\", "/")
        script = probe.as_posix()
        executable.with_suffix(".config.toml").write_text(
            f'target = "{target}"\n'
            "forward_arguments = true\n\n"
            "[[argument]]\n"
            f'value = "{script}"\n',
            encoding="utf-8",
        )
        return executable

    def run_wrapper(
        self, wrapper: Path, *arguments: str, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run a batch wrapper through the real Windows command interpreter."""
        return subprocess.run(
            ["cmd", "/c", str(wrapper), *arguments],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_gupkg_launcher_prefers_local_executable(self) -> None:
        """The outer launcher chooses its package-local command first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "gupkg"
            package.mkdir()
            local_probe = self.make_probe(root, "local")
            self.make_executable(package, "gupkg.exe", local_probe.with_suffix(".py"))
            path_dir = root / "path"
            path_dir.mkdir()
            path_probe = self.make_probe(root, "path")
            self.make_executable(path_dir, "gupkg.exe", path_probe.with_suffix(".py"))
            wrapper = root / "gupkg.cmd"
            shutil.copy2(SRC_ROOT / "gupkg.cmd", wrapper)

            env = os.environ.copy()
            env["PATH"] = f"{path_dir}{os.pathsep}{env.get('PATH', '')}"
            result = self.run_wrapper(
                wrapper, "--name", "value with spaces", cwd=root, env=env
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(local_probe.exists())
            self.assertFalse(path_probe.exists())
            recorded = local_probe.read_text(encoding="utf-8")
            self.assertEqual(
                ast.literal_eval(recorded.split("args=", 1)[1]),
                ["--name", "value with spaces"],
            )

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_tui_launcher_uses_local_tui_or_system_gupkg(self) -> None:
        """The TUI launcher selects local TUI and otherwise prepends ``tui``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "gupkg"
            package.mkdir()
            local_probe = self.make_probe(root, "local-tui")
            self.make_executable(
                package, "gupkg-tui.exe", local_probe.with_suffix(".py")
            )
            wrapper = root / "gupkg-tui.cmd"
            shutil.copy2(SRC_ROOT / "gupkg-tui.cmd", wrapper)

            env = os.environ.copy()
            result = self.run_wrapper(wrapper, "--probe", cwd=root, env=env)

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded = local_probe.read_text(encoding="utf-8")
            self.assertEqual(ast.literal_eval(recorded.split("args=", 1)[1]), ["--probe"])

            local_probe.unlink()
            (package / "gupkg-tui.exe").unlink()
            path_dir = root / "path"
            path_dir.mkdir()
            system_probe = self.make_probe(root, "system-tui")
            self.make_executable(path_dir, "gupkg.exe", system_probe.with_suffix(".py"))
            env["PATH"] = f"{path_dir}{os.pathsep}{env.get('PATH', '')}"
            result = self.run_wrapper(wrapper, "--probe", cwd=root, env=env)

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded = system_probe.read_text(encoding="utf-8")
            self.assertEqual(
                ast.literal_eval(recorded.split("args=", 1)[1]), ["tui", "--probe"]
            )

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_launcher_ignores_current_directory_fallback_and_preserves_failure(self) -> None:
        """PATH fallback excludes cwd executables and does not mask local failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrapper = root / "gupkg.cmd"
            shutil.copy2(SRC_ROOT / "gupkg.cmd", wrapper)
            current_probe = self.make_probe(root, "current")
            self.make_executable(root, "gupkg.exe", current_probe.with_suffix(".py"))
            env = os.environ.copy()
            env["PATH"] = ""

            result = self.run_wrapper(wrapper, "--probe", cwd=root, env=env)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(current_probe.exists())
            self.assertIn("No package-local or PATH", result.stderr)

            local = root / "gupkg"
            local.mkdir()
            failing_probe = self.make_probe(root, "failing", exit_code=23)
            self.make_executable(local, "gupkg.exe", failing_probe.with_suffix(".py"))
            fallback = root / "fallback"
            fallback.mkdir()
            fallback_probe = self.make_probe(root, "fallback")
            self.make_executable(
                fallback, "gupkg.exe", fallback_probe.with_suffix(".py")
            )
            env["PATH"] = f"{fallback}{os.pathsep}{env.get('PATH', '')}"

            result = self.run_wrapper(wrapper, "--probe", cwd=root, env=env)

            self.assertEqual(result.returncode, 23)
            self.assertFalse(fallback_probe.exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_outer_tui_launcher_falls_back_to_source_bootstrap(self) -> None:
        """The outer TUI launcher uses the adjacent bootstrap in a source checkout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "gupkg"
            package.mkdir()
            log_file = root / "source.log"
            native_probe = self.make_probe(root, "native-tui")
            self.make_executable(
                package, "gupkg-tui.exe", native_probe.with_suffix(".py")
            )
            source_tui = package / "gupkg-tui.cmd"
            source_tui.write_text(
                "@echo off\n"
                f'>> "{log_file}" echo args=%*\n'
                "exit /b 17\n",
                encoding="ascii",
            )
            wrapper = root / "gupkg-tui.cmd"
            shutil.copy2(SRC_ROOT / "gupkg-tui.cmd", wrapper)

            env = os.environ.copy()
            env["PATH"] = ""
            result = self.run_wrapper(wrapper, "--probe", cwd=root, env=env)

            self.assertEqual(result.returncode, 17, msg=result.stderr or result.stdout)
            self.assertEqual(
                log_file.read_text(encoding="utf-8").strip(), "args=--probe"
            )
            self.assertFalse(native_probe.exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_internal_bootstrap_handoff_keeps_python_policy_inside(self) -> None:
        """The internal command selects Python and hands dispatch to Python bootstrap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            internal = root / "gupkg"
            internal.mkdir()
            shutil.copy2(SRC_ROOT / "gupkg" / "gupkg.cmd", internal / "gupkg.cmd")
            shutil.copy2(SRC_ROOT / "gupkg" / "bootstrap.py", internal / "bootstrap.py")
            log_file = root / "python.log"
            fake_python = root / "fake-python.cmd"
            fake_python.write_text(
                f'''@echo off
> "{log_file}" echo cwd=%CD%
>> "{log_file}" echo args=%*
exit /b 0
''',
                encoding="ascii",
            )
            env = os.environ.copy()
            env["GUPKG_PYTHON"] = str(fake_python)

            result = self.run_wrapper(
                internal / "gupkg.cmd", "upgrade", "check", cwd=root, env=env
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            lines = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], f"cwd={root}")
            recorded = lines[1]
            self.assertIn("bootstrap.py", recorded)
            self.assertIn('"upgrade"', recorded)
            self.assertIn('"check"', recorded)

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_external_python_ignores_inherited_embedded_marker(self) -> None:
        """An external interpreter cannot be reclassified as the bundled runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            internal = root / "gupkg"
            internal.mkdir()
            shutil.copy2(SRC_ROOT / "gupkg" / "gupkg.cmd", internal / "gupkg.cmd")
            shutil.copy2(SRC_ROOT / "gupkg" / "bootstrap.py", internal / "bootstrap.py")
            log_file = root / "external-python.log"
            external_python = root / "external-python.cmd"
            external_python.write_text(
                f'''@echo off
> "{log_file}" echo args=%*
exit /b 0
''',
                encoding="ascii",
            )
            env = os.environ.copy()
            env["GUPKG_PYTHON"] = str(external_python)
            env["GUPKG_EMBEDDED"] = "1"
            env["GUPKG_BUNDLED_RUNTIME"] = "1"

            result = self.run_wrapper(
                internal / "gupkg.cmd", "inventory", cwd=root, env=env
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded = log_file.read_text(encoding="utf-8")
            self.assertIn("bootstrap.py", recorded)
            self.assertNotIn("--embedded", recorded)
            self.assertFalse((internal / "python").exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_internal_bootstrap_uses_windows_py_launcher_before_downloading(self) -> None:
        """An installed Python exposed through ``py -3`` prevents runtime bootstrap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            internal = root / "gupkg"
            internal.mkdir()
            shutil.copy2(SRC_ROOT / "gupkg" / "gupkg.cmd", internal / "gupkg.cmd")
            shutil.copy2(SRC_ROOT / "gupkg" / "bootstrap.py", internal / "bootstrap.py")
            log_file = root / "py.log"
            py_launcher = root / "py.cmd"
            py_launcher.write_text(
                f'''@echo off
if "%~1"=="-3" shift
if "%~1"=="-c" exit /b 0
> "{log_file}" echo args=%*
exit /b 0
''',
                encoding="ascii",
            )
            env = os.environ.copy()
            env.pop("GUPKG_PYTHON", None)
            env["PATH"] = str(root)

            result = self.run_wrapper(
                internal / "gupkg.cmd", "tui", cwd=root, env=env
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded = log_file.read_text(encoding="utf-8")
            self.assertIn("bootstrap.py", recorded)
            self.assertIn("tui", recorded)
            self.assertNotIn("--embedded", recorded)
            self.assertFalse((internal / "python").exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_internal_bootstrap_creates_runtime_directory_before_downloading(self) -> None:
        """A missing runtime directory is created before the downloader writes its archive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            internal = root / "gupkg"
            internal.mkdir()
            shutil.copy2(SRC_ROOT / "gupkg" / "gupkg.cmd", internal / "gupkg.cmd")
            shutil.copy2(SRC_ROOT / "gupkg" / "bootstrap.py", internal / "bootstrap.py")
            downloader_log = root / "downloader.log"
            runtime_log = root / "runtime.log"
            runtime_probe = self.make_probe(root, "runtime")
            downloader = root / "downloader.py"
            downloader.write_text(
                f'''import json
import os
from pathlib import Path
import shutil
import sys

runtime = Path(os.environ["GUPKG_BOOTSTRAP_DIRECTORY"])
Path({downloader_log.as_posix()!r}).write_text(str(runtime.is_dir()), encoding="utf-8")
if not runtime.is_dir():
    raise SystemExit(1)
shutil.copy2(Path({SHIM.as_posix()!r}), runtime / "python.exe")
(runtime / "python.config.toml").write_text(
    "target = " + json.dumps(sys.executable.replace("\\\\", "/")) + "\\n"
    + "forward_arguments = true\\n\\n"
    + "[[argument]]\\n"
    + "value = " + json.dumps({runtime_probe.with_suffix(".py").as_posix()!r}) + "\\n",
    encoding="utf-8",
)
''',
                encoding="utf-8",
            )
            powershell_directory = root / "System32" / "WindowsPowerShell" / "v1.0"
            powershell_directory.mkdir(parents=True)
            self.make_executable(powershell_directory, "powershell.exe", downloader)

            env = os.environ.copy()
            env.pop("GUPKG_PYTHON", None)
            env["PATH"] = ""
            env["SystemRoot"] = str(root)

            result = self.run_wrapper(
                internal / "gupkg.cmd", "tui", cwd=root, env=env
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertEqual(downloader_log.read_text(encoding="utf-8"), "True")
            recorded = runtime_log.read_text(encoding="utf-8")
            self.assertIn("bootstrap.py", recorded)
            self.assertIn("--embedded", recorded)
            self.assertNotIn("--embedded-root", recorded)

    @unittest.skipUnless(os.name == "nt", "Windows native shim behavior")
    def test_native_shim_calls_absolute_batch_without_changing_cwd(self) -> None:
        """A shell shim can launch a batch file without changing package cwd."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "package with spaces" / "gupkg"
            package.mkdir(parents=True)
            log_file = root / "batch.log"
            batch = package / "gupkg.cmd"
            batch.write_text(
                "@echo off\n"
                f'>"{log_file}" echo cwd=%CD%\n'
                f'>>"{log_file}" echo arg1=[%1] arg1tilde=[%~1] arg2=[%2] args=[%*]\n'
                "exit /b 0\n",
                encoding="ascii",
            )

            executable = root / "gupkg.exe"
            shutil.copy2(SHIM, executable)
            config = executable.with_suffix(".config.toml")
            config.write_text(
                'target = "%COMSPEC%"\n'
                "forward_arguments = true\n"
                "elevate = false\n\n"
                "[[argument]]\nvalue = \"/d\"\n\n"
                "[[argument]]\nvalue = \"/s\"\n\n"
                "[[argument]]\nvalue = \"/c\"\n\n"
                f"[[argument]]\nvalue = {json.dumps(str(batch))}\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(executable), "--probe"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            recorded = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(recorded[0], f"cwd={root}")
            self.assertEqual(
                recorded[1],
                'arg1=["--probe] arg1tilde=[--probe] arg2=[] args=["--probe]',
            )


if __name__ == "__main__":
    unittest.main()
