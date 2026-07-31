"""Install missing package-local hook dependencies into a user virtual environment.

Trusted ``pkg.local`` hooks can import third-party Python packages without
changing the interpreter that launches ``pkg``. When a hook reports a missing
module, this module installs the corresponding distribution into
``%LOCALAPPDATA%\\pkg\\dependencies`` and makes that environment available to
the current process. It prefers ``uv`` when it is on ``PATH`` and otherwise
uses the environment's ``pip``.
"""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any, Callable


def run_with_missing_dependencies(callback: Callable[..., Any], *args: Any) -> Any:
    """Run a hook and install missing third-party imports before retrying it.

    Parameters
    ----------
    callback : Callable[..., Any]
        Trusted package-local hook to invoke.
    *args : Any
        Positional arguments forwarded to *callback*.

    Returns
    -------
    Any
        The value returned by *callback*.

    Raises
    ------
    ModuleNotFoundError
        If a hook continues to report missing imports after installation.
    RuntimeError
        If the user dependency environment cannot be prepared or populated.
    """
    # A hook might import several independent dependencies, but a finite retry
    # limit prevents an invalid import name from repeatedly invoking installers.
    for _ in range(3):
        try:
            return callback(*args)
        except ModuleNotFoundError as exc:
            dependency = exc.name
            if not dependency:
                raise

            # Install only the missing top-level import because package indexes
            # identify distributions at that level rather than by dotted module.
            install_missing_dependency(dependency.split(".", maxsplit=1)[0])
    raise RuntimeError("A package-local hook required more than three missing dependencies")


def install_missing_dependency(module_name: str) -> None:
    """Install one importable dependency into pkg's per-user environment.

    Parameters
    ----------
    module_name : str
        Top-level import name reported by Python.

    Raises
    ------
    RuntimeError
        If virtual-environment creation or dependency installation fails.
    """
    distribution = _distribution_name(module_name)
    environment = _dependency_environment()
    python = _environment_python(environment)

    # Create the reusable environment before selecting an installer so pip is
    # always isolated from the Python interpreter that launched pkg.
    if not python.exists():
        print(f"[pkg] Creating dependency environment: {environment}")
        venv.EnvBuilder(with_pip=True).create(environment)

    # Make already installed dependencies immediately importable on retries.
    _add_environment_site_packages(python)
    if _module_is_importable(module_name):
        return

    # uv resolves and installs faster when available; pip remains a portable
    # fallback that operates only inside pkg's user-owned virtual environment.
    uv = shutil.which("uv")
    command = (
        [uv, "pip", "install", "--python", str(python), distribution]
        if uv
        else [str(python), "-m", "pip", "install", distribution]
    )
    installer = "uv" if uv else "pip"
    print(f"[pkg] Installing missing dependency with {installer}: {distribution}")
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Could not install missing dependency {distribution!r} with {installer}"
        )

    _add_environment_site_packages(python)
    if not _module_is_importable(module_name):
        raise RuntimeError(
            f"Installed {distribution!r}, but Python still cannot import {module_name!r}"
        )


def _dependency_environment() -> Path:
    """Return the user-owned virtual environment used by package-local hooks."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "pkg" / "dependencies"
    return Path.home() / "AppData" / "Local" / "pkg" / "dependencies"


def _environment_python(environment: Path) -> Path:
    """Return the Python executable path for one virtual environment."""
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _add_environment_site_packages(python: Path) -> None:
    """Add the dependency environment's site-packages directory to this process."""
    # Ask the environment itself for its site path so the host Python version
    # and platform layout cannot cause us to guess incorrectly.
    completed = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"Could not locate site-packages for {python}")
    site.addsitedir(completed.stdout.strip())


def _module_is_importable(module_name: str) -> bool:
    """Return whether the current process can import one module name."""
    try:
        __import__(module_name)
    except ModuleNotFoundError:
        return False
    return True


def _distribution_name(module_name: str) -> str:
    """Return the usual package-index distribution name for an import name."""
    # These projects intentionally expose import names different from their
    # package-index distribution names; ordinary imports install as themselves.
    aliases = {
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "dateutil": "python-dateutil",
        "yaml": "PyYAML",
    }
    return aliases.get(module_name, module_name)
