from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER_SCRIPTS = [
    (ROOT / "install.cmd", ["--pause"]),
    (ROOT / "install-machine.cmd", ["--scope", "Machine", "--pause"]),
    (ROOT / "update-config.cmd", ["--action", "UpdateConfig", "--pause"]),
]


class WrapperScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch wrapper behavior")
    def test_wrapper_scripts_preserve_caller_working_directory(self) -> None:
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
                    env["PKG_PYTHON"] = str(fake_python)
                    result = subprocess.run(
                        ["cmd", "/c", str(script)],
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
                    self.assertEqual(Path(recorded_cwd), version_dir)
                    self.assertIn(str(ROOT / "pkg.py"), recorded_args)
                    for arg in forwarded_args:
                        self.assertIn(arg, recorded_args)


if __name__ == "__main__":
    unittest.main()
