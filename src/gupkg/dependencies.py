"""Install gupkg runtime dependencies while protecting package-local hooks.

The ``gupkg`` runtime installs its declared third-party dependencies into a
per-user site-packages directory without changing the interpreter that launches
``gupkg``. Package-local hooks do not install imports by default: callers must
explicitly opt in before their missing modules can be installed. A normal
interpreter installs into ``%LOCALAPPDATA%\\gupkg\\site-packages``; the bundled
embeddable CPython installs into
``%LOCALAPPDATA%\\gupkg\\embedded\\site-packages``. Each directory is added to
the current process before pip is invoked.
"""

from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


class MissingLocalDependencyError(RuntimeError):
    """Report a package-local dependency that gupkg deliberately did not install."""


# Keep each optional gupkg feature's trusted dependencies explicit and auditable.
_RUNTIME_DEPENDENCIES = {"tui": ("textual",)}


def ensure_runtime_dependencies(feature: str) -> None:
    """Make every declared dependency for one gupkg feature importable.

    Parameters
    ----------
    feature : str
        Name of the gupkg feature whose declared dependencies are required.
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
    """Make one runtime dependency importable by the current gupkg process.

    Parameters
    ----------
    module_name : str
        Top-level Python import required by gupkg itself.

    Raises
    ------
    RuntimeError
        If the isolated dependency environment cannot install the dependency.
    """
    if not _module_is_importable(module_name):
        install_missing_dependency(module_name)


def install_missing_dependency(module_name: str) -> None:
    """Install one importable dependency into gupkg's per-user environment.

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
    site_packages = _dependency_site_packages()

    # Keep normal and bundled interpreters in separate per-user directories so
    # their independently installed packages never overwrite one another.
    site_packages.mkdir(parents=True, exist_ok=True)
    site.addsitedir(str(site_packages))
    if _module_is_importable(module_name):
        return

    # Target the owned directory directly instead of changing the selected
    # interpreter. The bundled runtime includes pip for this exact workflow.
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        distribution,
    ]

    # Invoke the selected installer only after the dependency directory has
    # been put on the current process's import path for immediate retrying.
    print(f"[gupkg] Installing missing dependency with pip: {distribution}")
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Could not install missing dependency {distribution!r} with pip")

    site.addsitedir(str(site_packages))
    importlib.invalidate_caches()
    if not _module_is_importable(module_name):
        raise RuntimeError(
            f"Installed {distribution!r}, but Python still cannot import {module_name!r}"
        )


def _dependency_site_packages() -> Path:
    """Return the per-user package directory for the active interpreter kind."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_directory = Path(local_app_data) / "gupkg"
    else:
        base_directory = Path.home() / "AppData" / "Local" / "gupkg"
    if _uses_embedded_python():
        return base_directory / "embedded" / "site-packages"
    return base_directory / "site-packages"


def _uses_embedded_python() -> bool:
    """Return whether the active interpreter is CPython's embeddable distribution."""
    executable_directory = Path(sys.executable).resolve().parent
    pth_name = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    return (executable_directory / pth_name).is_file()


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
