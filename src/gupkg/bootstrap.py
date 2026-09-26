"""Prepare an embedded runtime and dispatch the public gupkg command.

The bootstrap entry point is used after batch code has selected an interpreter.
It owns Python-readable runtime support files and the embedded-runtime pip
bootstrap, while ordinary interpreter selection remains in the local command
file.

Usage and API
-------------
Call ``main(...)`` from the internal launcher. It prepares an embedded runtime
when requested and forwards the remaining arguments to ``gupkg.gupkg.main``.

Implementation Approach
-----------------------
The bootstrap establishes import paths from its own package location, writes
the small runtime support files needed by an embeddable interpreter, and then
hands the original command arguments to the existing Python CLI. System Python
invocations skip embedded-runtime mutation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from urllib.request import urlopen


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare the selected runtime and return the public CLI exit status.

    Parameters
    ----------
    argv : sequence of str, optional
        Bootstrap options followed by the arguments intended for ``gupkg``.

    Returns
    -------
    int
        The exit status returned by the public CLI.

    Raises
    ------
    RuntimeError
        If embedded-runtime support files or pip cannot be prepared.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--embedded", action="store_true")
    parser.add_argument("--root", type=Path)
    options, command_args = parser.parse_known_args(argv)

    # Make the sibling package importable regardless of the caller's cwd.
    package_parent = Path(__file__).resolve().parent.parent
    if sys.path[:1] != [str(package_parent)]:
        sys.path.insert(0, str(package_parent))

    if options.embedded:
        _prepare_embedded_runtime(Path(sys.executable).resolve().parent)

    # Preserve the existing root contract while keeping bootstrap-only flags
    # out of the public command parser.
    from gupkg.gupkg import main as gupkg_main

    public_args = list(command_args)
    if options.root is not None:
        public_args[0:0] = ["--root", str(options.root)]
    return int(gupkg_main(public_args))


def _prepare_embedded_runtime(runtime_directory: Path) -> None:
    """Write embedded-runtime support files and ensure pip is available."""
    # Keep the embedded interpreter isolated while allowing the adjacent app
    # package and its private dependency directory to be imported.
    pth_name = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    pth_path = runtime_directory / pth_name
    pth_path.write_text(
        "python312.zip\n.\n..\nLib/site-packages\n\nimport site\n",
        encoding="ascii",
    )
    _write_sitecustomize()
    _ensure_pip()


def _write_sitecustomize() -> None:
    """Write the site hook that exposes mutable embedded dependencies."""
    sitecustomize_path = Path(sys.executable).resolve().parent / "sitecustomize.py"
    sitecustomize_path.write_text(
        '''"""Add gupkg's mutable embedded dependency directory to sys.path."""

from __future__ import annotations

import os
import site
from pathlib import Path

if not os.environ.get("GUPKG_BOOTSTRAPPING_PIP"):
    local_app_data = os.environ.get("LOCALAPPDATA")
    pip_target = (
        Path(local_app_data) / "gupkg" / "embedded" / "site-packages"
        if local_app_data
        else Path.home() / "AppData" / "Local" / "gupkg" / "embedded" / "site-packages"
    )
    site.addsitedir(str(pip_target))
    os.environ.setdefault("PIP_TARGET", str(pip_target))
''',
        encoding="utf-8",
    )


def _ensure_pip() -> None:
    """Install pip into the embedded runtime's mutable dependency location."""
    # Avoid importing pip into the bootstrap process before sitecustomize is
    # active; the subprocess observes the same support files on its next run.
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return

    local_app_data = os.environ.get("LOCALAPPDATA")
    pip_target = (
        Path(local_app_data) / "gupkg" / "embedded" / "site-packages"
        if local_app_data
        else Path.home() / "AppData" / "Local" / "gupkg" / "embedded" / "site-packages"
    )
    pip_target.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix="-get-pip.py", delete=False) as handle:
        installer_path = Path(handle.name)
    try:
        with urlopen("https://bootstrap.pypa.io/get-pip.py") as response:
            installer_path.write_bytes(response.read())
        environment = os.environ.copy()
        environment["GUPKG_BOOTSTRAPPING_PIP"] = "1"
        environment["PIP_TARGET"] = str(pip_target)
        result = subprocess.run(
            [sys.executable, str(installer_path), "--no-warn-script-location"],
            env=environment,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Could not install pip for embedded Python.")
    finally:
        installer_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"[gupkg] {error}", file=sys.stderr)
        raise SystemExit(1)
