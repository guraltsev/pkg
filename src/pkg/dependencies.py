"""Install pkg runtime dependencies while protecting package-local hooks.

The ``pkg`` runtime installs its declared third-party dependencies into an
isolated user environment without changing the interpreter that launches
``pkg``. Package-local hooks do not install imports by default: callers must
explicitly opt in before their missing modules can be installed into
``%LOCALAPPDATA%\\pkg\\dependencies`` and makes that environment available to
the current process. It prefers ``uv`` when it is on ``PATH`` and otherwise
uses the environment's ``pip``.
"""

from __future__ import annotations

import importlib
import os
import shutil
import site
import subprocess
import venv
from collections.abc import Callable
from pathlib import Path
from typing import Any


class MissingLocalDependencyError(RuntimeError):
    """Report a package-local dependency that pkg deliberately did not install."""


# Keep each optional pkg feature's trusted dependencies explicit and auditable.
_RUNTIME_DEPENDENCIES = {"tui": ("textual",)}


def ensure_runtime_dependencies(feature: str) -> None:
    """Make every declared dependency for one pkg feature importable.

    Parameters
    ----------
    feature : str
        Name of the pkg feature whose declared dependencies are required.
    """
    for module_name in _RUNTIME_DEPENDENCIES.get(feature, ()):
        ensure_dependency(module_name)


def run_with_missing_dependencies(
    callback: Callable[..., Any], *args: Any, autoinstall: bool = False
) -> Any:
    """Run a hook and optionally install missing third-party imports before retrying it.

    Parameters
    ----------
    callback : Callable[..., Any]
        Trusted package-local hook to invoke.
    *args : Any
        Positional arguments forwarded to *callback*.
    autoinstall : bool, default=False
        Whether missing package-local imports may be installed before retrying.

    Returns
    -------
    Any
        The value returned by *callback*.

    Raises
    ------
    MissingLocalDependencyError
        If a hook needs an unavailable import and automatic installation is off.
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

            # Package-local code is trusted but still package-owned. Do not let
            # it trigger network installs unless the caller explicitly opted in.
            if not autoinstall:
                raise MissingLocalDependencyError(
                    f"Package-local dependency unavailable: {dependency}. "
                    "Install it yourself or rerun with --local-deps-autoinstall."
                ) from exc

            # Install only the missing top-level import because package indexes
            # identify distributions at that level rather than by dotted module.
            install_missing_dependency(dependency.split(".", maxsplit=1)[0])
    raise RuntimeError("A package-local hook required more than three missing dependencies")


def ensure_dependency(module_name: str) -> None:
    """Make one runtime dependency importable by the current pkg process.

    Parameters
    ----------
    module_name : str
        Top-level Python import required by pkg itself.

    Raises
    ------
    RuntimeError
        If the isolated dependency environment cannot install the dependency.
    """
    if not _module_is_importable(module_name):
        install_missing_dependency(module_name)


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
    importlib.invalidate_caches()
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
        [
            str(python),
            "-c",
            (
                "import site; print(next(path for path in site.getsitepackages() "
                "if path.lower().endswith('site-packages')))"
            ),
        ],
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
