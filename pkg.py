#!/usr/bin/env python3
r"""gurlatsev/pkg — Local Package Manager for Windows

User-facing functionality
-------------------------

``pkg`` installs and configures locally cached Windows applications that follow
the package layout used by this project. From a user perspective, it does three
main things:

1) Picks the version to activate by updating the package-level ``current``
   junction to the selected version directory.
2) Applies package configuration from ``pkg.toml``, including Start Menu
   shortcuts, environment variables, PATH entries, and wrapper files in a
   per-scope ``bin`` directory.
3) Supports both ``User`` and ``Machine`` installation scopes, so packages can
   be installed per-user or system-wide (with admin rights for Machine scope).

The default action is ``Install``. ``Install`` does not auto-create ``pkg.toml`` when it is missing; it uses runtime defaults only. ``UpdateConfig`` is the explicit action that creates a starter config or synchronizes filesystem-derived metadata while preserving comments and unknown keys.

Examples
--------

Typical invocations:

::

    pkg
    pkg C:\opt\pkgs\Ripgrep\v14.1.0.l1
    pkg C:\opt\pkgs\Ripgrep

Expected package layout:

::

    <pkg_name>/
      current/                (NTFS junction)
      v1.2.3.l1/
        App/
        Icons/
        Shortcuts/
        pkg.toml

Minimal ``pkg.toml`` snippet (note that ``${VAR}`` is the recommended environment-expansion syntax):

::

    name = "Ripgrep"
    version = "14.1.0"
    localVersion = 1

    [[shortcut]]
    name = "Ripgrep"
    targetPath = "$App\\rg.exe"

    [[environment]]
    Name = "RIPGREP_HOME"
    Value = "$App"

    [[path]]
    value = "$App"

Architecture and design
-----------------------

This script installs *locally cached* Windows applications from a standardized
directory layout. An installation typically:

1) Updates the package-level ``current`` *junction* so it points at the chosen
   version directory.
2) Creates Start Menu shortcuts for the package.
3) Sets package-specific environment variables.
4) Ensures a per-scope ``bin`` directory exists and is on ``PATH``.
5) Writes small executable/wrapper files into that ``bin`` directory.

The code is organized as a set of small, single-purpose components coordinated
by :class:`PackageManager`. Startup is side-effect free: help/version do not create folders or install dependencies.


- :class:`PackageMetadata`
    Parses the directory naming convention (``v<upstream>.l<local>``) and loads
    the per-version config file (``pkg.toml``). It also computes paths used
    throughout installation.

- :class:`JunctionManager`
    Creates/validates NTFS junctions and compares version strings to decide
    whether ``current`` should be moved to a newer version.

- :class:`VariableExpander`
    Performs variable expansion in configuration strings. It supports package
    variables (``$App``, ``$Icons``, ``$Shortcuts``) everywhere, supports
    ``${VAR}`` environment expansion everywhere, and keeps plain ``$VAR``
    expansion restricted to general config fields so wrapper scripts preserve
    shell-native variables like PowerShell's ``$PSScriptRoot`` and ``$args``.
    In script content, plain ``$VAR`` is treated as literal text unless it is a
    package variable such as ``$App``.


- :class:`ShortcutInstaller`
    Creates ``.lnk`` shortcuts in the Start Menu. Uses ``pywin32`` if available,
    otherwise falls back to PowerShell automation.

- :class:`EnvironmentVariableManager`
    Writes environment variables to the Windows registry (User or Machine scope).

- :class:`PATHManager`
    Reads/writes the registry-backed ``Path`` value and appends entries
    idempotently (avoiding duplicates).

- :class:`BinFileCreator`
    Creates simple wrapper files inside a per-scope ``bin`` directory.

Execution flow
~~~~~~~~~~~~~~

``main()`` parses CLI arguments, constructs :class:`PackageManager`, normalizes
the provided path, then performs one of the actions:

- ``Install`` (default): update ``current`` junction if needed, then run the install pipeline for shortcuts/env/PATH/bin wrappers. Missing ``pkg.toml`` uses defaults and writes nothing.
- ``UpdateConfig``: sync filesystem-derived metadata into ``pkg.toml`` while
  preserving comments and unknown keys when an existing file is updated.


Directory layout
~~~~~~~~~~~~~~~~

A package lives under a *package directory* ``<pkg_name>`` with one or more
*version directories*:

::

    <pkg_name>/
      current/                (NTFS junction)
      v1.2.3.l1/
        App/
        Icons/
        Shortcuts/
        pkg.toml

Version directories must be named ``v<upstream>.l<local>``, for example:
``v1.2.3.l1`` or ``v1.2-beta.3.l4``.

Configuration schema (pkg.toml)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Top-level keys used by this tool:

- ``name`` (str): must match the directory name.
- ``version`` (str): upstream part (without leading ``v`` or local revision).
- ``localVersion`` (int|str): local revision (the ``lN`` part).
- ``only_portable`` (bool): if true, disallow Machine installs.
- ``environment`` (list[dict]): items with keys ``Name`` and ``Value``.
- ``path`` (list[dict]): extra directories to append to PATH.
  Use repeated ``[[path]]`` tables with a required ``value`` key.
- ``shortcut`` (list[dict]): shortcut definitions (see extended help).
- ``bin`` (list[dict]): wrapper definitions with keys ``name`` and ``content``.

Safety and scope
~~~~~~~~~~~~~~~~

Exit codes:
- ``0`` success (including no-op success)
- ``2`` user/config/input/dependency problem
- ``3`` system mutation failure
- ``4`` unexpected internal failure


- Machine scope modifies HKLM environment variables and requires Administrator
  privileges.
- Start Menu shortcuts are installed under the per-scope Start Menu directory:
  ``%APPDATA%\...\Start Menu\opt`` (User) or
  ``%PROGRAMDATA%\...\Start Menu\opt`` (Machine).
- PATH modifications are made in the registry and may require a logoff/logon for
  all processes to observe them.

"""

from __future__ import annotations

# =============================================================================
# Imports and optional backend loaders
# =============================================================================

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

if os.name == "nt":
    import winreg
else:
    winreg = None  # type: ignore[assignment]


def try_import(module_name: str) -> Optional[Any]:
    """Import *module_name* if available, otherwise return ``None``."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def get_win32com_client() -> Optional[Any]:
    """Return ``win32com.client`` when available without installing anything."""
    return try_import("win32com.client")


def require_winreg() -> Any:
    """Return the ``winreg`` module or raise a clear platform error."""
    if winreg is None:
        raise OSError("winreg is only available on Windows.")
    return winreg


# =============================================================================
# User-facing documentation, version, and constants
# =============================================================================

__version__ = "0.12.0"
__copyright__ = "Copyright (C) 2025 Gennady Uraltseev. All rights reserved."
__license__ = "MIT"

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4


EXTENDED_HELP = r"""
Extended help
-------------

Quick start
~~~~~~~~~~~

Notes:
  - ``pkg --help`` and ``pkg --version`` do not install dependencies or write files.
  - ``Install`` does not auto-create ``pkg.toml``.
  - ``UpdateConfig`` preserves comments, unknown keys, and existing TOML structure when updating an existing file.

Run the tool from inside a *version directory*:

  pkg                 # installs from the current working directory

Or pass a path to a version directory:

  pkg C:\opt\pkgs\Ripgrep\v14.1.0.l1

You may also pass the *package root* (the directory that contains ``current``);
in that case the tool installs from the ``current`` junction:

  pkg C:\opt\pkgs\Ripgrep

Scopes
~~~~~~

User scope:
  - shortcuts: %APPDATA%\Microsoft\Windows\Start Menu\opt
  - PATH/env: HKCU\Environment
  - bin dir:  %USERPROFILE%\bin

Machine scope (requires admin):
  - shortcuts: %PROGRAMDATA%\Microsoft\Windows\Start Menu\opt
  - PATH/env:  HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment
  - bin dir:   <SYSTEMDRIVE>\bin

Config keys and examples
~~~~~~~~~~~~~~~~~~~~~~~~

1) Shortcuts (``shortcut`` list)
   Each entry is a dict. Supported keys:

   - name (required): file name without extension or with .lnk
   - targetPath (required; alias: path): executable path
   - arguments (optional)
   - workingDirectory (optional)
   - iconLocation (optional): e.g. "C:\path\icon.ico,0"
   - description (optional)

   Example (TOML):

     [[shortcut]]
     name = "My App"
     targetPath = "$App\\MyApp.exe"
     arguments = ""
     workingDirectory = "$App"
     iconLocation = "$Icons\\myapp.ico,0"
     description = "Launch My App"

2) Environment variables (``environment`` list)
   Keys are case-insensitive (canonical: ``Name`` and ``Value``):

     [[environment]]
     Name = "MYAPP_HOME"
     Value = "$App"

3) PATH additions (``path`` list)
   Each entry is appended (if not already present). Entries may include $-vars.
   Use repeated ``[[path]]`` tables (only):

     [[path]]
     value = "$App"

     [[path]]
     value = "$App\\bin"

4) Bin wrappers (``bin`` list)
   Each entry is a dict with keys:

   - name (required): file name under the bin dir (e.g. "myapp.cmd")
   - content (required): full file content after variable expansion

   Syntax notes:

   - ``content`` is the full script text written to the wrapper file.
   - Use a TOML multi-line *literal* string (triple single quotes) for scripts
     that include newlines, quotes, and backslashes.
   - Package variables like ``$App`` are expanded before writing the file.
   - Plain ``$VAR`` stays literal inside wrapper content (except package
     variables). Use ``${VAR}`` when you explicitly want environment expansion
     inside a script.

   Example (CMD wrapper with expanded ``$App``):

     [[bin]]
     name = "myapp.cmd"
     content = '''
@echo off
python "$App\\app_script_name.py" %*
'''

   If you use TOML basic strings instead, escape quotes and backslashes
   explicitly (e.g. ``\"`` and ``\\``).
   In particular, Windows paths like ``$App\bin\gh.exe`` must be written as
   ``$App\\bin\\gh.exe`` in basic strings, or placed inside a literal
   multi-line string to avoid TOML "reserved escape sequence" parse errors.
   Also, if you keep content on one line (TOML basic string), use
   escaped newlines (``\n``), which pkg now normalizes to real line breaks.

   PowerShell example:

     [[bin]]
     name = "myapp.ps1"
     content = "\"\"\"
$ErrorActionPreference = 'Stop'
$app = Join-Path $PSScriptRoot '..\opt\MyApp\MyApp.exe'
& $app @args
\"\"\"

Variable expansion rules
~~~~~~~~~~~~~~~~~~~~~~~~

- Package variables are expanded everywhere:
    $App, $Icons, $Shortcuts
  These resolve through the stable ``current`` junction path (e.g.
  ``<pkg>\current\App``). The junction does not need to exist for simple
  string substitution.

- Environment variables use two modes:
    - ``${VAR}`` is supported everywhere.
    - plain ``$VAR`` is supported only in general config fields such as
      shortcuts, environment values, and PATH entries.
    - plain ``$VAR`` inside wrapper content stays literal unless it is one of
      the package variables above.

- Unresolved variables are treated as failures for mutable steps. In
  particular, missing variables in PATH entries, shortcut fields, and registry
  environment values are not silently rewritten to empty strings.

- Escaping:
    Use ``$$`` to produce a literal ``$`` (for example, ``$$App`` becomes
    the literal text ``$App`` rather than expanding to a path).

Notes
~~~~~

- After registry PATH changes, existing terminals won't automatically see the
  new PATH. Open a new terminal or log off/on.
- TOML support:
    - No Python packages are auto-installed by pkg.
    - Read-only config parsing prefers stdlib ``tomllib`` on Python 3.11+, then ``tomlkit``, then ``toml``.
    - Updating an existing ``pkg.toml`` while preserving comments and formatting requires ``tomlkit``.
    - If ``pkg.toml`` is missing during Install, pkg uses defaults and does not create a file.
- Exit codes:
    - ``0`` success (including no-op success)
    - ``2`` user/config/input/dependency problem
    - ``3`` system mutation failure
    - ``4`` unexpected internal failure
"""


# =============================================================================
# Pure helpers
#   - version parsing/comparison
#   - path classification
#   - atomic file helpers
#   - reporting/result helpers
# =============================================================================

VERSION_DIR_NAME_RE = re.compile(r"^v(.+)\.l(\d+)$")

TOP_LEVEL_CONFIG_KEY_ALIASES: Dict[str, str] = {
    "name": "name",
    "version": "version",
    "localversion": "localVersion",
    "local_version": "localVersion",
    "description": "description",
    "homepage": "homepage",
    "downloadurl": "downloadURL",
    "download_url": "downloadURL",
    "environment": "environment",
    "env": "environment",
    "bin": "bin",
    "path": "path",
    "shortcut": "shortcut",
    "shortcuts": "shortcut",
    "only_portable": "only_portable",
    "onlyportable": "only_portable",
    "portable": "portable",
    "main": "main",
}
MAIN_TABLE_KEY_ALIASES: Dict[str, str] = {
    "name": "name",
    "version": "version",
    "localversion": "localVersion",
    "local_version": "localVersion",
    "description": "description",
    "homepage": "homepage",
    "downloadurl": "downloadURL",
    "download_url": "downloadURL",
    "only_portable": "only_portable",
    "onlyportable": "only_portable",
    "portable": "portable",
}
ENVIRONMENT_KEY_ALIASES: Dict[str, str] = {"name": "Name", "value": "Value"}
BIN_KEY_ALIASES: Dict[str, str] = {"name": "name", "content": "content"}
SHORTCUT_KEY_ALIASES: Dict[str, str] = {
    "name": "name",
    "targetpath": "targetPath",
    "target_path": "targetPath",
    "path": "targetPath",
    "arguments": "arguments",
    "args": "arguments",
    "workingdirectory": "workingDirectory",
    "working_directory": "workingDirectory",
    "workdir": "workingDirectory",
    "iconlocation": "iconLocation",
    "icon_location": "iconLocation",
    "description": "description",
    "desc": "description",
}
PATH_ENTRY_KEY_ALIASES: Dict[str, str] = {"value": "value", "path": "value"}
CONFIG_LIST_FIELDS = ("environment", "bin", "path", "shortcut")
OWNED_METADATA_FIELDS = ("name", "version", "localVersion", "only_portable")
OWNED_METADATA_KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("name",),
    "version": ("version",),
    "localVersion": ("localVersion", "localversion", "local_version"),
    "only_portable": ("only_portable", "onlyportable", "portable"),
}
PACKAGE_VARIABLE_NAMES = ("App", "Icons", "Shortcuts")


def is_version_directory_name(name: str) -> bool:
    return VERSION_DIR_NAME_RE.match(name) is not None


def split_package_version(version: str) -> Tuple[str, int]:
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    if ".l" in version:
        upstream_part, local_part = version.rsplit(".l", 1)
        try:
            local_revision = int(local_part)
        except ValueError:
            local_revision = 0
    else:
        upstream_part = version
        local_revision = 0
    return upstream_part, local_revision


def compare_package_versions(version1: str, version2: str) -> int:
    def parse_identifier(token: str) -> Union[int, str]:
        token = token.strip()
        if token.isdigit():
            return int(token)
        return token.lower()

    def parse_upstream(upstream: str) -> Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]:
        upstream = upstream.strip()
        if "+" in upstream:
            upstream = upstream.split("+", 1)[0]
        if "-" in upstream:
            main_part, prerelease_part = upstream.split("-", 1)
            prerelease = [parse_identifier(part) for part in prerelease_part.split(".") if part != ""]
        else:
            main_part = upstream
            prerelease = None
        main = [parse_identifier(part) for part in main_part.split(".") if part != ""]
        return main, prerelease

    def compare_identifier_lists(left: List[Union[int, str]], right: List[Union[int, str]], *, pad_numeric: bool) -> int:
        max_len = max(len(left), len(right))
        for index in range(max_len):
            left_value = left[index] if index < len(left) else (0 if pad_numeric else None)
            right_value = right[index] if index < len(right) else (0 if pad_numeric else None)
            if left_value == right_value:
                continue
            if left_value is None:
                return -1
            if right_value is None:
                return 1
            if isinstance(left_value, int) and isinstance(right_value, int):
                return 1 if left_value > right_value else -1
            if isinstance(left_value, int) and isinstance(right_value, str):
                return -1
            if isinstance(left_value, str) and isinstance(right_value, int):
                return 1
            return 1 if str(left_value) > str(right_value) else -1
        return 0

    upstream1, local1 = split_package_version(version1)
    upstream2, local2 = split_package_version(version2)
    main1, prerelease1 = parse_upstream(upstream1)
    main2, prerelease2 = parse_upstream(upstream2)
    main_comparison = compare_identifier_lists(main1, main2, pad_numeric=True)
    if main_comparison != 0:
        return main_comparison
    if prerelease1 is None and prerelease2 is not None:
        return 1
    if prerelease1 is not None and prerelease2 is None:
        return -1
    if prerelease1 is not None and prerelease2 is not None:
        prerelease_comparison = compare_identifier_lists(prerelease1, prerelease2, pad_numeric=False)
        if prerelease_comparison != 0:
            return prerelease_comparison
    if local1 == local2:
        return 0
    return 1 if local1 > local2 else -1


def normalize_path(path: Union[str, Path]) -> Path:
    r"""Normalize a filesystem path for reliable comparisons.

    On Windows, junction/symlink targets may be returned in extended-length form
    (``\\?\`` prefix). This helper strips that prefix (if present) and
    returns a resolved :class:`~pathlib.Path`.

    Args:
        path: Input path as a string or :class:`~pathlib.Path`.

    Returns:
        A resolved :class:`~pathlib.Path` without the extended-length prefix.

    """
    path_str = str(path)
    if path_str.startswith('\\\\?\\'):
        path_str = path_str[4:]
    return Path(path_str).resolve()


def get_shortcut_backend() -> str:
    """Return the shortcut backend that should be used at runtime."""
    return "pywin32" if get_win32com_client() is not None else "powershell"


def load_toml_reader() -> TomlReader:
    """Load a read-only TOML backend lazily when config parsing is needed."""
    tomllib_module = try_import("tomllib")
    if tomllib_module is not None:
        return TomlReader("tomllib", tomllib_module)

    tomlkit_module = try_import("tomlkit")
    if tomlkit_module is not None:
        return TomlReader("tomlkit", tomlkit_module)

    toml_module = try_import("toml")
    if toml_module is not None:
        return TomlReader("toml", toml_module)

    raise DependencyError(
        "No TOML reader is available. Run pkg with Python 3.11+ (stdlib tomllib), "
        "or install tomlkit or toml before using config-driven actions."
    )


def load_roundtrip_toml_backend(require: bool) -> Optional[RoundTripBackend]:
    """Load the round-trip TOML backend lazily when existing configs are updated."""
    tomlkit_module = try_import("tomlkit")
    if tomlkit_module is not None:
        return RoundTripBackend("tomlkit", tomlkit_module)
    return None


def read_toml_file(path: Path) -> Dict[str, Any]:
    """Read TOML into plain Python data using a lazily selected backend."""
    reader = load_toml_reader()
    if reader.name == "tomllib":
        with open(path, "rb") as f:
            return reader.module.load(f)
    if reader.name == "tomlkit":
        return reader.module.parse(path.read_text(encoding="utf-8")).unwrap()
    with open(path, "r", encoding="utf-8") as f:
        return reader.module.load(f)


def write_text_atomic(path: Path, text: str, *, backup: bool = False) -> None:
    """Atomically write text to *path*, optionally creating ``.bak`` first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as fh:
            tmp_fd = None
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Atomically write bytes to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(tmp_fd, "wb") as fh:
            tmp_fd = None
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# =============================================================================
# Enums, dataclasses, and custom exceptions
# =============================================================================

class ConfigValidationError(ValueError):
    """Raised when pkg.toml configuration is invalid."""


class DependencyError(RuntimeError):
    """Raised when an optional backend is required but unavailable."""


class Scope(Enum):
    """Installation scope.

    - ``USER``: per-user install (registry under HKCU, Start Menu under APPDATA)
    - ``MACHINE``: system-wide install (registry under HKLM, Start Menu under PROGRAMDATA)

    """

    USER = "User"
    MACHINE = "Machine"


class Action(Enum):
    """CLI actions supported by this tool."""

    INSTALL = "Install"
    UPDATE_CONFIG = "UpdateConfig"


@dataclass(frozen=True)
class TomlReader:
    """Lazy TOML reader backend."""

    name: str
    module: Any


@dataclass(frozen=True)
class RoundTripBackend:
    """Lazy round-trip TOML backend."""

    name: str
    module: Any


@dataclass
class TextConfigDocument:
    """Fallback metadata-only TOML editor used when tomlkit is unavailable."""

    text: str

    def as_string(self) -> str:
        return self.text


@dataclass
class StepResult:
    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class ActionResult:
    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    exit_code: int = EXIT_SUCCESS


@dataclass(frozen=True)
class ResolvedInput:
    raw_path: Path
    package_root: Path
    version_path: Path
    input_kind: str
    installing_from_current: bool


@dataclass(frozen=True)
class PackageIdentity:
    name: str
    version: str
    local_version: int
    version_string: str
    package_root: Path
    version_path: Path
    is_current: bool
    only_portable_by_name: bool

    @classmethod
    def from_resolved_input(cls, resolved: "ResolvedInput") -> "PackageIdentity":
        match = VERSION_DIR_NAME_RE.match(resolved.version_path.name)
        if not match:
            raise ValueError(
                f"Invalid version directory name: {resolved.version_path.name}. Expected format: v<upstream>.l<local>"
            )
        package_root = resolved.package_root
        version_path = resolved.version_path
        current_path = package_root / "current"
        is_current = False
        if current_path.exists() and JunctionManager.is_junction(current_path):
            target = JunctionManager.get_junction_target(current_path)
            if target is not None:
                try:
                    is_current = normalize_path(target) == normalize_path(version_path)
                except OSError:
                    is_current = False
        return cls(
            name=package_root.name,
            version=match.group(1),
            local_version=int(match.group(2)),
            version_string=version_path.name,
            package_root=package_root,
            version_path=version_path,
            is_current=is_current,
            only_portable_by_name=package_root.name.lower().endswith("-portable"),
        )


@dataclass(frozen=True)
class ScopePaths:
    scope: Scope
    shortcut_root: Path
    bin_dir: Path
    env_root: Any
    env_subkey: str


@dataclass
class ShortcutSpec:
    name: str
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


@dataclass
class EnvVarSpec:
    name: str
    value: str


@dataclass
class BinSpec:
    name: str
    content: str


@dataclass
class PackageConfig:
    description: Optional[str] = None
    homepage: Optional[str] = None
    download_url: Optional[str] = None
    only_portable: bool = False
    environment: List[EnvVarSpec] = field(default_factory=list)
    shortcut: List[ShortcutSpec] = field(default_factory=list)
    path: List[str] = field(default_factory=list)
    bin: List[BinSpec] = field(default_factory=list)


@dataclass
class ExpansionResult:
    value: str
    unresolved: List[str] = field(default_factory=list)


class ExpansionMode(Enum):
    GENERAL = "general"
    SCRIPT = "script"


@dataclass(frozen=True)
class PreparedShortcut:
    name: str
    shortcut_path: Path
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


@dataclass(frozen=True)
class InstallContext:
    identity: PackageIdentity
    config: PackageConfig
    scope_paths: ScopePaths
    reporter: Any
    force: bool


class Reporter:
    """Minimal console reporter used by the orchestration layer."""

    def info(self, msg: str) -> None:
        print(msg)

    def warn(self, msg: str) -> None:
        if msg.startswith("WARNING:"):
            print(msg)
        else:
            print(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        if msg.startswith("ERROR:"):
            print(msg)
        else:
            print(f"ERROR: {msg}")


def combine_step_results(*results: StepResult) -> StepResult:
    """Combine several step results into one aggregate result."""
    combined = StepResult(ok=True, changed=False)
    for result in results:
        combined.ok = combined.ok and result.ok
        combined.changed = combined.changed or result.changed
        combined.warnings.extend(result.warnings)
        combined.errors.extend(result.errors)
    if combined.errors:
        combined.ok = False
    return combined


# =============================================================================
# Package identity and scope paths
# =============================================================================

def resolve_input_path(raw_path: Path) -> ResolvedInput:
    """Classify the user input path before resolving any junction targets."""
    candidate = raw_path.expanduser()

    if is_version_directory_name(candidate.name):
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"Version directory does not exist: {candidate}")
        return ResolvedInput(
            raw_path=candidate,
            package_root=candidate.parent,
            version_path=candidate,
            input_kind="version",
            installing_from_current=False,
        )

    if candidate.name.lower() == "current":
        if not candidate.exists():
            raise ValueError(f'"current" path does not exist: {candidate}')
        if not JunctionManager.is_junction(candidate):
            raise ValueError(
                f'"current" path exists but is not a valid junction: {candidate}; '
                f"exists={candidate.exists()}, is_dir={candidate.is_dir()}, parent={candidate.parent}"
            )
        target = JunctionManager.get_junction_target(candidate)
        if target is None:
            raise ValueError(f'Could not resolve "current" junction target: {candidate}')
        resolved_target = normalize_path(target)
        if not resolved_target.is_dir():
            raise ValueError(
                f'"current" junction target is not a directory: {resolved_target}; source={candidate}, raw_target={target}'
            )
        return ResolvedInput(
            raw_path=candidate,
            package_root=candidate.parent,
            version_path=resolved_target,
            input_kind="current",
            installing_from_current=True,
        )

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Package root does not exist: {candidate}")
    current_path = candidate / "current"
    if not current_path.exists():
        raise ValueError(
            f'No "current" directory exists in package root: {candidate}; '
            f"looked for {current_path}; root_exists={candidate.exists()}, root_is_dir={candidate.is_dir()}"
        )
    if not JunctionManager.is_junction(current_path):
        raise ValueError(
            f'"current" path exists but is not a valid junction: {current_path}; '
            f"exists={current_path.exists()}, is_dir={current_path.is_dir()}, parent={current_path.parent}"
        )
    target = JunctionManager.get_junction_target(current_path)
    if target is None:
        raise ValueError(f'Could not resolve "current" junction target: {current_path}')
    resolved_target = normalize_path(target)
    if not resolved_target.is_dir():
        raise ValueError(
            f'"current" junction target is not a directory: {resolved_target}; source={current_path}, raw_target={target}'
        )
    return ResolvedInput(
        raw_path=candidate,
        package_root=candidate,
        version_path=resolved_target,
        input_kind="package_root",
        installing_from_current=True,
    )


def compute_scope_paths(scope: Scope) -> ScopePaths:
    registry = winreg
    if scope == Scope.USER:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ValueError("APPDATA is not set; cannot compute User-scope shortcut directory.")
        userprofile = os.environ.get("USERPROFILE")
        if not userprofile:
            raise ValueError("USERPROFILE is not set; cannot compute User-scope bin directory.")
        return ScopePaths(
            scope=scope,
            shortcut_root=Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "opt",
            bin_dir=Path(userprofile) / "bin",
            env_root=(registry.HKEY_CURRENT_USER if registry is not None else None),
            env_subkey="Environment",
        )

    programdata = os.environ.get("PROGRAMDATA")
    if not programdata:
        raise ValueError("PROGRAMDATA is not set; cannot compute Machine-scope shortcut directory.")
    systemdrive = os.environ.get("SYSTEMDRIVE")
    if not systemdrive:
        raise ValueError("SYSTEMDRIVE is not set; cannot compute Machine-scope bin directory.")
    return ScopePaths(
        scope=scope,
        shortcut_root=Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "opt",
        bin_dir=Path(systemdrive) / "bin",
        env_root=(registry.HKEY_LOCAL_MACHINE if registry is not None else None),
        env_subkey=r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )


# =============================================================================
# Runtime config model and validation
# =============================================================================

def normalize_runtime_config(raw: Any, identity: PackageIdentity) -> PackageConfig:
    """Normalize runtime config into a dedicated runtime model."""
    default_data: Dict[str, Any] = {
        "name": identity.name,
        "version": identity.version,
        "localVersion": identity.local_version,
        "description": None,
        "homepage": None,
        "downloadURL": None,
        "environment": [],
        "bin": [],
        "path": [],
        "shortcut": [],
        "only_portable": identity.only_portable_by_name,
    }
    merged = dict(default_data)
    if raw is not None:
        if not isinstance(raw, dict):
            raise ConfigValidationError(f"Configuration must be a dict, got: {type(raw).__name__}")
        merged.update(PackageMetadata._canonicalize_config_dict(dict(raw)))
    normalized = PackageMetadata._canonicalize_config_dict(merged)
    return PackageConfig(
        description=normalized.get("description"),
        homepage=normalized.get("homepage"),
        download_url=normalized.get("downloadURL"),
        only_portable=bool(normalized.get("only_portable", identity.only_portable_by_name)),
        environment=[
            EnvVarSpec(name=str(item.get("Name", "")), value=str(item.get("Value", "")))
            for item in normalized.get("environment", [])
        ],
        shortcut=[
            ShortcutSpec(
                name=str(item.get("name", "")),
                target_path=str(item.get("targetPath", "")),
                arguments=str(item.get("arguments", "")),
                working_directory=str(item.get("workingDirectory", "")),
                icon_location=str(item.get("iconLocation", "")),
                description=str(item.get("description", "")),
            )
            for item in normalized.get("shortcut", [])
        ],
        path=[str(item) for item in normalized.get("path", [])],
        bin=[
            BinSpec(name=str(item.get("name", "")), content=str(item.get("content", "")))
            for item in normalized.get("bin", [])
        ],
    )


def validate_runtime_config(config: PackageConfig) -> None:
    errors: List[str] = []
    for i, shortcut in enumerate(config.shortcut):
        missing = []
        if not shortcut.name.strip():
            missing.append("name")
        if not shortcut.target_path.strip():
            missing.append("targetPath")
        if missing:
            errors.append(f"shortcut[{i}] missing required key(s): {', '.join(missing)}")
    for i, env in enumerate(config.environment):
        missing = []
        if not env.name.strip():
            missing.append("Name")
        if env.value == "":
            missing.append("Value")
        if missing:
            errors.append(f"environment[{i}] missing required key(s): {', '.join(missing)}")
    for i, wrapper in enumerate(config.bin):
        missing = []
        if not wrapper.name.strip():
            missing.append("name")
        if wrapper.content == "":
            missing.append("content")
        if missing:
            errors.append(f"bin[{i}] missing required key(s): {', '.join(missing)}")
    if errors:
        joined = "\n  - " + "\n  - ".join(errors)
        raise ConfigValidationError(f"Invalid configuration:{joined}")


def package_config_to_dict(config: PackageConfig, identity: PackageIdentity) -> Dict[str, Any]:
    return {
        "name": identity.name,
        "version": identity.version,
        "localVersion": identity.local_version,
        "description": config.description,
        "homepage": config.homepage,
        "downloadURL": config.download_url,
        "only_portable": config.only_portable,
        "environment": [{"Name": item.name, "Value": item.value} for item in config.environment],
        "shortcut": [
            {
                "name": item.name,
                "targetPath": item.target_path,
                "arguments": item.arguments,
                "workingDirectory": item.working_directory,
                "iconLocation": item.icon_location,
                "description": item.description,
            }
            for item in config.shortcut
        ],
        "path": list(config.path),
        "bin": [{"name": item.name, "content": item.content} for item in config.bin],
    }


def check_metadata_consistency(identity: PackageIdentity, raw_or_config: Any) -> List[str]:
    if isinstance(raw_or_config, PackageConfig):
        data = {
            "name": identity.name,
            "version": identity.version,
            "localVersion": identity.local_version,
            "only_portable": raw_or_config.only_portable,
        }
    elif isinstance(raw_or_config, dict):
        data = PackageMetadata._canonicalize_config_dict(dict(raw_or_config))
    else:
        raise TypeError("raw_or_config must be a PackageConfig or dict")

    inconsistencies: List[str] = []
    if "name" in data and data.get("name") not in (None, "") and str(data.get("name")) != identity.name:
        inconsistencies.append(f"Name mismatch: directory='{identity.name}', config='{data.get('name')}'")
    if "version" in data and data.get("version") not in (None, "") and str(data.get("version")) != identity.version:
        inconsistencies.append(f"Version mismatch: directory='{identity.version}', config='{data.get('version')}'")
    if "localVersion" in data and data.get("localVersion") not in (None, "") and str(data.get("localVersion")) != str(identity.local_version):
        inconsistencies.append(
            f"LocalVersion mismatch: directory='{identity.local_version}', config='{data.get('localVersion')}'"
        )
    if "only_portable" in data and data.get("only_portable") is not None and bool(data.get("only_portable")) != identity.only_portable_by_name:
        inconsistencies.append(
            f"Portable flag mismatch: directory='{identity.only_portable_by_name}', config='{bool(data.get('only_portable'))}'"
        )
    return inconsistencies


def read_runtime_config(identity: PackageIdentity, use_defaults: bool = False) -> Tuple[PackageConfig, Dict[str, Any], List[str]]:
    warnings: List[str] = []
    toml_path = identity.version_path / "pkg.toml"
    if toml_path.exists():
        try:
            loaded = read_toml_file(toml_path)
            config = normalize_runtime_config(loaded, identity)
            validate_runtime_config(config)
            return config, PackageMetadata._canonicalize_config_dict(dict(loaded)), warnings
        except DependencyError:
            if not use_defaults:
                raise
            warnings.append(
                "No TOML reader is available for pkg.toml parsing. Proceeding with defaults because --use-defaults was provided."
            )
        except ConfigValidationError:
            raise
        except Exception as e:
            if not use_defaults:
                raise RuntimeError(f"Error loading TOML config from {toml_path}: {e}") from e
            warnings.append(f"Error loading TOML config from {toml_path}: {e}")
            warnings.append("Proceeding with defaults because --use-defaults was provided.")
    config = normalize_runtime_config({}, identity)
    validate_runtime_config(config)
    warnings.append(f"No pkg.toml found at {toml_path}; using defaults without creating a file.")
    return config, package_config_to_dict(config, identity), warnings


class PackageMetadata:
    """Compatibility facade over the newer resolved-input, identity, and config helpers."""

    def __init__(self, version_path: Path):
        resolved = resolve_input_path(Path(version_path))
        self.resolved_input = resolved
        self.identity = PackageIdentity.from_resolved_input(resolved)
        self.input_path = resolved.raw_path
        self.version_path = self.identity.version_path
        self.pkg_path = self.identity.package_root
        self.name = self.identity.name
        self.version = self.identity.version
        self.local_version = str(self.identity.local_version)
        self.version_string = self.identity.version_string
        self.is_current = self.identity.is_current
        self.scope: Scope = Scope.USER
        self.scope_paths: Optional[ScopePaths] = None
        self.shortcut_dir: Path = Path()
        self.only_portable: bool = self.identity.only_portable_by_name
        self.description: Optional[str] = None
        self.homepage: Optional[str] = None
        self.download_url: Optional[str] = None
        self.environment: List[Dict[str, str]] = []
        self.bin: List[Dict[str, str]] = []
        self.path: List[str] = []
        self.shortcut: List[Dict[str, str]] = []
        self.runtime_config: Optional[PackageConfig] = None

    def _fill_from_directory(self) -> None:
        return None

    def _fill_current(self) -> None:
        return None

    def check_metadata_consistency(self, config_data: Dict[str, Any]) -> List[str]:
        return check_metadata_consistency(self.identity, config_data)


    # ---------------------------------------------------------------------
    # Configuration normalization and validation
    # ---------------------------------------------------------------------

    @staticmethod
    def _canonicalize_dict_keys(data: Dict[str, Any], keymap: Dict[str, str], *, context: str) -> Dict[str, Any]:
        """Return a copy of *data* with keys normalized in a case-insensitive way.

        Keys are matched by ``str(key).lower()`` against *keymap* and rewritten to
        the canonical spelling stored in *keymap*. Unknown keys are preserved.

        This function also detects collisions such as ``{"Name": ..., "name": ...}``
        that would map to the same canonical key.

        Args:
            data: Input dictionary.
            keymap: Mapping from lower-cased key -> canonical key name.
            context: Human-readable context string used in error messages.

        Returns:
            A new dictionary with canonicalized keys.

        Raises:
            ConfigValidationError: If two different original keys map to the same
                canonical key.
        """
        out: Dict[str, Any] = {}
        seen_from: Dict[str, str] = {}

        for k, v in data.items():
            k_str = str(k)
            canonical = keymap.get(k_str.lower(), k_str)

            if canonical in out and seen_from.get(canonical) != k_str:
                raise ConfigValidationError(
                    f"Duplicate keys differing only by case/alias in {context}: "
                    f"'{seen_from.get(canonical)}' and '{k_str}' both map to '{canonical}'."
                )

            out[canonical] = v
            seen_from[canonical] = k_str

        return out

    @staticmethod
    def _normalize_bin_content(value: Any) -> str:
        """Normalize wrapper content so escaped newlines behave as users expect.

        If a value contains literal escape sequences like ``\n``/``\r\n`` but no
        actual newline characters, convert those escapes to real newlines. This
        keeps one-line TOML strings usable for multi-line wrapper scripts.
        """
        text = str(value or "")
        if "\n" in text:
            return text
        return text.replace("\\r\\n", "\n").replace("\\n", "\n")

    @staticmethod
    def _canonicalize_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        """Canonicalize known config keys (case-insensitive) and normalize shapes.

        This makes TOML keys case-insensitive for the supported schema. In
        particular it normalizes:

        - top-level keys like ``Name``/``NAME`` -> ``name``
        - shortcut entry keys like ``TargetPath``/``path`` -> ``targetPath``
        - environment entry keys like ``name`` -> ``Name`` and ``value`` -> ``Value``

        Returns:
            A new dict with canonicalized keys and list fields normalized to lists.

        Raises:
            ConfigValidationError: If the config structure is not as expected.
        """
        if not isinstance(data, dict):
            raise ConfigValidationError(f"Configuration must be a dict, got: {type(data).__name__}")

        out = PackageMetadata._canonicalize_dict_keys(
            data,
            TOP_LEVEL_CONFIG_KEY_ALIASES,
            context="config",
        )

        # Support a dedicated [[main]] TOML section for package-level metadata.
        main_block = out.pop("main", None)
        if main_block is not None:
            if not isinstance(main_block, list):
                raise ConfigValidationError(f"'main' must be a list, got: {type(main_block).__name__}")
            if len(main_block) > 1:
                raise ConfigValidationError("'main' must contain a single table")
            if len(main_block) == 1:
                if not isinstance(main_block[0], dict):
                    raise ConfigValidationError(
                        f"'main[0]' must be a dict, got: {type(main_block[0]).__name__}"
                    )
                normalized_main = PackageMetadata._canonicalize_dict_keys(
                    main_block[0],
                    MAIN_TABLE_KEY_ALIASES,
                    context="main[0]",
                )
                for key, value in normalized_main.items():
                    out[key] = value

        # Normalize absent/null list fields.
        for k in CONFIG_LIST_FIELDS:
            if out.get(k, None) is None:
                out[k] = []

        # Be tolerant in what we accept:
        #   - canonical TOML form: repeated [[path]] tables
        #   - legacy/internal form: list[str]
        #   - convenience form: a single string
        if isinstance(out.get("path", None), str):
            out["path"] = [out["path"]]
        elif not isinstance(out.get("path", []), list):
            raise ConfigValidationError(
                f"'path' must be a list of strings or [[path]] tables, got: {type(out['path']).__name__}"
            )

        # Canonicalize list-of-dict blocks.
        def canonicalize_block(block_name: str, keymap: Dict[str, str]) -> List[Dict[str, Any]]:
            raw = out.get(block_name, [])
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise ConfigValidationError(f"'{block_name}' must be a list, got: {type(raw).__name__}")
            result: List[Dict[str, Any]] = []
            for i, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise ConfigValidationError(
                        f"'{block_name}[{i}]' must be a dict, got: {type(item).__name__}"
                    )
                result.append(PackageMetadata._canonicalize_dict_keys(item, keymap, context=f"{block_name}[{i}]"))
            return result

        out["environment"] = canonicalize_block("environment", ENVIRONMENT_KEY_ALIASES)
        out["bin"] = canonicalize_block("bin", BIN_KEY_ALIASES)
        for bw in out["bin"]:
            if "content" in bw:
                bw["content"] = PackageMetadata._normalize_bin_content(bw.get("content", ""))
        out["shortcut"] = canonicalize_block("shortcut", SHORTCUT_KEY_ALIASES)

        # Normalize path entries.
        # We canonicalize to List[str] internally regardless of whether the
        # config used [[path]] tables or a list of strings.
        normalized_path: List[str] = []
        for i, entry in enumerate(out.get("path", [])):
            if entry is None:
                continue
            if isinstance(entry, str):
                normalized_path.append(entry)
                continue
            if isinstance(entry, dict):
                path_item = PackageMetadata._canonicalize_dict_keys(entry, PATH_ENTRY_KEY_ALIASES, context=f"path[{i}]")
                value = path_item.get("value", None)
                if value is None:
                    raise ConfigValidationError(
                        f"'path[{i}]' table is missing required key: value (present keys: {', '.join(sorted(str(k) for k in path_item.keys()))})"
                    )
                if not isinstance(value, str):
                    raise ConfigValidationError(f"'path[{i}].value' must be a string, got: {type(value).__name__}")
                normalized_path.append(value)
                continue
            raise ConfigValidationError(
                f"'path[{i}]' must be a string or a dict ([[path]] table), got: {type(entry).__name__}"
            )
        out["path"] = normalized_path

        return out

    @staticmethod
    def _validate_config_dict(data: Dict[str, Any]) -> None:
        """Validate mandatory keys for list entries (shortcuts/env/bin).

        This is a *sanity check* to prevent subtle bugs (e.g. creating an
        ``opt.lnk`` shortcut when ``name`` is missing). See
        :class:`ShortcutInstaller` for additional runtime guards.

        Raises:
            ConfigValidationError: If required fields are missing.
        """
        errors: List[str] = []

        def keys_str(d: Dict[str, Any]) -> str:
            return ", ".join(sorted(str(k) for k in d.keys()))

        # Shortcuts: require name + targetPath
        shortcuts = data.get("shortcut", [])
        if isinstance(shortcuts, list):
            for i, sc in enumerate(shortcuts):
                if not isinstance(sc, dict):
                    errors.append(f"shortcut[{i}] must be a dict")
                    continue
                name = str(sc.get("name", "") or "").strip()
                target = str(sc.get("targetPath", "") or "").strip()
                if not name or not target:
                    missing = []
                    if not name:
                        missing.append("name")
                    if not target:
                        missing.append("targetPath")
                    errors.append(
                        f"shortcut[{i}] missing required key(s): {', '.join(missing)} "
                        f"(present keys: {keys_str(sc)})"
                    )

        # Environment variables: require Name + Value
        envs = data.get("environment", [])
        if isinstance(envs, list):
            for i, ev in enumerate(envs):
                if not isinstance(ev, dict):
                    errors.append(f"environment[{i}] must be a dict")
                    continue
                n = str(ev.get("Name", "") or "").strip()
                v = str(ev.get("Value", "") or "").strip()
                if not n or v == "":
                    missing = []
                    if not n:
                        missing.append("Name")
                    if v == "":
                        missing.append("Value")
                    errors.append(
                        f"environment[{i}] missing required key(s): {', '.join(missing)} "
                        f"(present keys: {keys_str(ev)})"
                    )

        # Bin wrappers: require name + content
        bins = data.get("bin", [])
        if isinstance(bins, list):
            for i, bw in enumerate(bins):
                if not isinstance(bw, dict):
                    errors.append(f"bin[{i}] must be a dict")
                    continue
                n = str(bw.get("name", "") or "").strip()
                c = str(bw.get("content", "") or "")
                if not n or c == "":
                    missing = []
                    if not n:
                        missing.append("name")
                    if c == "":
                        missing.append("content")
                    errors.append(
                        f"bin[{i}] missing required key(s): {', '.join(missing)} "
                        f"(present keys: {keys_str(bw)})"
                    )

        if errors:
            joined = "\n  - " + "\n  - ".join(errors)
            raise ConfigValidationError(f"Invalid configuration:{joined}")

    @staticmethod
    def _to_toml_scalar(value: Any) -> str:
        """Compatibility wrapper around the shared TOML rendering helper."""
        return _to_toml_scalar(value)

    def _metadata_sync_payload(self) -> Dict[str, Any]:
        """Compatibility wrapper around the shared metadata sync payload helper."""
        return metadata_sync_payload(self.identity)

    def _create_starter_config_text(self, data: Optional[Dict[str, Any]] = None) -> str:
        """Compatibility wrapper around the shared starter-config helper."""
        return create_starter_config(self.identity)

    @staticmethod
    def _locate_metadata_container(doc: Any) -> Any:
        """Compatibility wrapper around the shared document-location helper."""
        return locate_metadata_container(doc)

    def load_config(self, *, use_defaults: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        config, _raw_data, warnings = read_runtime_config(self.identity, use_defaults=use_defaults)
        self.runtime_config = config
        self.description = config.description
        self.homepage = config.homepage
        self.download_url = config.download_url
        self.environment = [{"Name": item.name, "Value": item.value} for item in config.environment]
        self.bin = [{"name": item.name, "content": item.content} for item in config.bin]
        self.path = list(config.path)
        self.shortcut = [
            {
                "name": item.name,
                "targetPath": item.target_path,
                "arguments": item.arguments,
                "workingDirectory": item.working_directory,
                "iconLocation": item.icon_location,
                "description": item.description,
            }
            for item in config.shortcut
        ]
        self.only_portable = config.only_portable
        return package_config_to_dict(config, self.identity), warnings

    def _load_from_dict(self, data: Dict[str, Any]) -> None:
        config = normalize_runtime_config(data, self.identity)
        validate_runtime_config(config)
        self.runtime_config = config
        self.description = config.description
        self.homepage = config.homepage
        self.download_url = config.download_url
        self.environment = [{"Name": item.name, "Value": item.value} for item in config.environment]
        self.bin = [{"name": item.name, "content": item.content} for item in config.bin]
        self.path = list(config.path)
        self.shortcut = [
            {
                "name": item.name,
                "targetPath": item.target_path,
                "arguments": item.arguments,
                "workingDirectory": item.working_directory,
                "iconLocation": item.icon_location,
                "description": item.description,
            }
            for item in config.shortcut
        ]
        self.only_portable = config.only_portable

    def update_config(self, reporter: Optional[Reporter] = None) -> StepResult:
        """Synchronize only the owned metadata fields back to ``pkg.toml``.

        Existing files are edited in place with a round-trip TOML backend so
        comments, unknown keys, and existing table structure are preserved.
        Missing files are created only for this explicit action.
        """
        reporter = reporter or Reporter()
        toml_path = self.version_path / "pkg.toml"
        json_path = self.version_path / "pkg.json"
        warnings: List[str] = []

        if toml_path.exists():
            doc, original_text = load_config_document(toml_path)
            changed = sync_document_metadata(doc, self.identity)
            rendered = doc.as_string()
            if not changed or rendered == original_text:
                reporter.info(f"Configuration already up to date: {toml_path}")
                result = StepResult(ok=True, changed=False)
            else:
                write_text_atomic(toml_path, rendered, backup=True)
                reporter.info(f"Updated: {toml_path}")
                result = StepResult(ok=True, changed=True)
        else:
            rendered = create_starter_config(self.identity)
            write_text_atomic(toml_path, rendered, backup=False)
            reporter.info(f"Created: {toml_path}")
            result = StepResult(ok=True, changed=True)

        if json_path.exists():
            try:
                json_path.unlink()
                reporter.info(f"Removed: {json_path} (legacy config; TOML-only)")
                result.changed = True
            except OSError as e:
                warning = f"Failed to remove legacy JSON config {json_path}: {e}"
                reporter.warn(warning)
                warnings.append(warning)

        result.warnings.extend(warnings)
        return result

    def set_scope(self, scope: Scope) -> None:
        """Set installation scope and compute shared scope paths."""
        self.scope = scope
        self.scope_paths = compute_scope_paths(scope)
        self.shortcut_dir = self.scope_paths.shortcut_root



# =============================================================================
# TOML document I/O and round-trip config updates
# =============================================================================

# Runtime config parsing still normalizes to dicts; existing-file updates use tomlkit round-trip edits.


def _to_toml_scalar(value: Any) -> str:
    """Render a scalar value as TOML literal text."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_to_toml_scalar(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def metadata_sync_payload(identity: PackageIdentity) -> Dict[str, Any]:
    """Return the directory-derived metadata owned by pkg."""
    return {
        "name": identity.name,
        "version": identity.version,
        "localVersion": identity.local_version,
        "only_portable": identity.only_portable_by_name,
    }


def load_config_document(path: Path) -> Tuple[Any, str]:
    """Load an existing ``pkg.toml`` with a round-trip backend when available.

    If ``tomlkit`` is unavailable, fall back to a narrow metadata-only text editor
    that preserves comments, unknown keys, and overall layout while syncing only
    the owned metadata keys.
    """
    original_text = path.read_text(encoding="utf-8")
    backend = load_roundtrip_toml_backend(require=False)
    if backend is not None:
        try:
            return backend.module.parse(original_text), original_text
        except Exception as e:
            raise ConfigValidationError(
                f"pkg.toml is structurally invalid and cannot be updated safely: {e}. Edit the config manually."
            ) from e
    return TextConfigDocument(original_text), original_text


def locate_metadata_container(doc: Any) -> Any:
    """Return the TOML object whose keys should be metadata-synced."""
    if isinstance(doc, TextConfigDocument):
        return doc
    main_block = doc.get("main", None)
    if main_block is None:
        return doc
    if not isinstance(main_block, list):
        raise ConfigValidationError(
            "Existing pkg.toml uses a malformed [[main]] section. Edit the config manually."
        )
    if len(main_block) != 1:
        raise ConfigValidationError(
            "Existing pkg.toml uses [[main]] but it does not contain exactly one table. Edit the config manually."
        )
    return main_block[0]


def _find_existing_metadata_key(container: Any, canonical_key: str) -> Optional[str]:
    """Find the existing key spelling/alias for a metadata field if present."""
    aliases = {alias.lower() for alias in OWNED_METADATA_KEY_ALIASES.get(canonical_key, (canonical_key,))}
    exact_match: Optional[str] = None
    alias_match: Optional[str] = None
    for key in container.keys():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_text == canonical_key:
            exact_match = key_text
            break
        if key_lower in aliases and alias_match is None:
            alias_match = key_text
    return exact_match or alias_match


def _sync_text_metadata(doc: TextConfigDocument, identity: PackageIdentity) -> bool:
    text = doc.text
    changed = False
    metadata = metadata_sync_payload(identity)

    main_matches = list(re.finditer(r'(?mi)^\s*\[\[main\]\]\s*$', text))
    if len(main_matches) > 1:
        raise ConfigValidationError(
            "Existing pkg.toml uses [[main]] but it does not contain exactly one table. Edit the config manually."
        )

    if main_matches:
        start = main_matches[0].end()
        next_table = re.search(r'(?m)^\s*\[\[?.*\]?\]\s*$', text[start:])
        end = start + next_table.start() if next_table else len(text)
        container_text = text[start:end]
        container_offset = start
    else:
        first_table = re.search(r'(?m)^\s*\[\[?.*\]?\]\s*$', text)
        end = first_table.start() if first_table else len(text)
        container_text = text[:end]
        container_offset = 0

    for canonical_key, value in metadata.items():
        aliases = OWNED_METADATA_KEY_ALIASES.get(canonical_key, (canonical_key,))
        alias_pattern = "|".join(re.escape(a) for a in aliases)
        pattern = re.compile(
            rf'(?mi)^(?P<indent>\s*)(?P<key>{alias_pattern})\s*=\s*(?P<value>[^\n#]*)(?P<comment>\s*(?:#.*)?)$'
        )
        match = pattern.search(container_text)
        rendered_value = _to_toml_scalar(value)
        if match:
            existing_value = match.group('value').strip()
            if existing_value != rendered_value:
                replacement_line = (
                    f"{match.group('indent')}{match.group('key')} = {rendered_value}{match.group('comment')}"
                )
                container_text = container_text[:match.start()] + replacement_line + container_text[match.end():]
                changed = True
        else:
            insertion = f"{canonical_key} = {rendered_value}\n"
            if container_text and not container_text.endswith(("\n", "\r")):
                container_text += "\n"
            container_text += insertion
            changed = True

    if changed:
        if container_offset == 0:
            doc.text = container_text + text[end:]
        else:
            doc.text = text[:container_offset] + container_text + text[end:]
    return changed

def sync_document_metadata(doc: Any, identity: PackageIdentity) -> bool:
    """Mutate only the owned metadata fields in an existing TOML document."""
    if isinstance(doc, TextConfigDocument):
        return _sync_text_metadata(doc, identity)
    container = locate_metadata_container(doc)
    changed = False
    for canonical_key, value in metadata_sync_payload(identity).items():
        existing_key = _find_existing_metadata_key(container, canonical_key)
        target_key = existing_key or canonical_key
        if container.get(target_key) != value:
            container[target_key] = value
            changed = True
    return changed


def create_starter_config(identity: PackageIdentity) -> str:
    """Create a minimal future-facing starter ``pkg.toml``."""
    metadata = metadata_sync_payload(identity)
    lines = [
        f"name = {_to_toml_scalar(metadata['name'])}"
        f"version = {_to_toml_scalar(metadata['version'])}"
        f"localVersion = {_to_toml_scalar(metadata['localVersion'])}"
        f"only_portable = {_to_toml_scalar(metadata['only_portable'])}"
    ]
    return "\n".join(lines).rstrip() + "\n"

# =============================================================================
# Variable expansion
# =============================================================================

def _deduplicate_preserving_order(values: List[str]) -> List[str]:
    """Return *values* without duplicates while preserving first-seen order."""
    seen = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _package_variable_map(identity: PackageIdentity) -> Dict[str, str]:
    """Return the package-variable expansion map for an identity."""
    pkg_base = identity.package_root / "current"
    return {
        "App": str(pkg_base / "App"),
        "Icons": str(pkg_base / "Icons"),
        "Shortcuts": str(pkg_base / "Shortcuts"),
    }


def expand_text(text: str, identity: PackageIdentity, mode: ExpansionMode) -> ExpansionResult:
    """Expand package/environment variables according to the selected mode.

    Rules:
    - ``$App``, ``$Icons``, and ``$Shortcuts`` expand everywhere.
    - ``${VAR}`` expands everywhere and is an error when unresolved.
    - plain ``$VAR`` expands only in :class:`ExpansionMode.GENERAL`.
    - plain ``$VAR`` remains literal in :class:`ExpansionMode.SCRIPT`, except
      for the package variables listed above.
    - ``$$`` becomes a literal ``$``.
    """
    if text is None:
        return ExpansionResult("")

    source = str(text)
    if source == "":
        return ExpansionResult("")

    pkg_map = _package_variable_map(identity)
    out: List[str] = []
    unresolved: List[str] = []
    i = 0
    while i < len(source):
        char = source[i]
        if char != "$":
            out.append(char)
            i += 1
            continue

        if i + 1 < len(source) and source[i + 1] == "$":
            out.append("$")
            i += 2
            continue

        if i + 1 < len(source) and source[i + 1] == "{":
            closing = source.find("}", i + 2)
            if closing == -1:
                out.append("$")
                i += 1
                continue
            var_name = source[i + 2 : closing]
            token = source[i : closing + 1]
            if var_name in os.environ:
                out.append(os.environ[var_name])
            else:
                out.append(token)
                unresolved.append(token)
            i = closing + 1
            continue

        if i + 1 < len(source) and re.match(r"[A-Za-z_]", source[i + 1]):
            j = i + 2
            while j < len(source) and re.match(r"[A-Za-z0-9_]", source[j]):
                j += 1
            var_name = source[i + 1 : j]
            token = source[i:j]
            if var_name in pkg_map:
                out.append(pkg_map[var_name])
            elif mode == ExpansionMode.GENERAL:
                if var_name in os.environ:
                    out.append(os.environ[var_name])
                else:
                    out.append(token)
                    unresolved.append(token)
            else:
                out.append(token)
            i = j
            continue

        out.append("$")
        i += 1

    return ExpansionResult("".join(out), _deduplicate_preserving_order(unresolved))


class VariableExpander:
    """Compatibility wrappers around the newer expansion API."""

    @staticmethod
    def expand_variables(
        text: str,
        metadata: PackageMetadata,
        mode: ExpansionMode = ExpansionMode.GENERAL,
    ) -> ExpansionResult:
        return expand_text(text, metadata.identity, mode)

    @staticmethod
    def expand_dict(
        data: Dict[str, str],
        metadata: PackageMetadata,
        mode: ExpansionMode = ExpansionMode.GENERAL,
    ) -> Dict[str, ExpansionResult]:
        return {key: expand_text(value, metadata.identity, mode) for key, value in data.items()}


# =============================================================================
# Windows backends
#   - junctions
#   - shortcuts
#   - registry env/path
# =============================================================================

class JunctionManager:
    """Create and inspect NTFS junctions used for version switching."""

    @staticmethod
    def create_junction(source: Path, target: Path) -> bool:
        r"""Create or replace an NTFS junction.

        This uses ``cmd /c mklink /J`` under the hood.

        Args:
            source: Path where the junction will be created (e.g. ``...\current``).
            target: Existing directory that the junction should point to.

        Returns:
            True if the junction was created successfully, False otherwise.

        """
        try:
            if not target.exists() or not target.is_dir():
                print(f"JUNCTION error: target does not exist or is not a directory: {target}")
                return False

            if os.path.lexists(str(source)):
                if JunctionManager.is_junction(source):
                    try:
                        # Junctions are directory reparse points. Use rmdir for
                        # consistent removal across Python/Windows versions.
                        os.rmdir(str(source))
                    except OSError as e:
                        print(f"JUNCTION error: failed to remove existing junction {source}: {e}")
                        return False
                else:
                    print(
                        f"JUNCTION error: {source} already exists and is not a junction; "
                        "refusing to overwrite."
                    )
                    return False

            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(source), str(target)],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            if result.returncode == 0:
                print(f"JUNCTION: created: {source.name} -> {target}")
                return True

            err = (result.stderr or "").strip()
            out = (result.stdout or "").strip()
            if err:
                print(f"JUNCTION error: {err}")
            if out:
                print(f"JUNCTION output: {out}")
            return False

        except Exception as e:
            print(f"JUNCTION error creating {source}: {e}")
            return False

    @staticmethod
    def _win_get_reparse_tag(path: Path) -> Optional[int]:
        """Return the reparse tag for *path* using Windows APIs.

        This avoids parsing localized/human output such as ``dir``.

        Returns:
            The integer reparse tag, or ``None`` if it cannot be determined.

        """
        try:
            import ctypes
            from ctypes import wintypes

            # CreateFileW flags for opening a directory reparse point.
            FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
            FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
            OPEN_EXISTING = 3
            FILE_SHARE_READ = 0x00000001
            FILE_SHARE_WRITE = 0x00000002
            FILE_SHARE_DELETE = 0x00000004

            FSCTL_GET_REPARSE_POINT = 0x000900A8

            CreateFileW = ctypes.windll.kernel32.CreateFileW
            CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            CreateFileW.restype = wintypes.HANDLE

            DeviceIoControl = ctypes.windll.kernel32.DeviceIoControl
            DeviceIoControl.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ]
            DeviceIoControl.restype = wintypes.BOOL

            CloseHandle = ctypes.windll.kernel32.CloseHandle
            CloseHandle.argtypes = [wintypes.HANDLE]
            CloseHandle.restype = wintypes.BOOL

            handle = CreateFileW(
                str(path),
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
            if handle == INVALID_HANDLE_VALUE:
                return None

            try:
                buf = ctypes.create_string_buffer(16 * 1024)
                returned = wintypes.DWORD(0)
                ok = DeviceIoControl(
                    handle,
                    FSCTL_GET_REPARSE_POINT,
                    None,
                    0,
                    buf,
                    len(buf),
                    ctypes.byref(returned),
                    None,
                )
                if not ok:
                    return None
                # REPARSE_DATA_BUFFER starts with ULONG ReparseTag.
                return int.from_bytes(buf.raw[0:4], "little", signed=False)
            finally:
                CloseHandle(handle)
        except Exception:
            return None

    @staticmethod
    def is_junction(path: Path) -> bool:
        """Return True if *path* is an NTFS junction (reparse point).

        Args:
            path: Directory path to test.

        Returns:
            True if the path exists and is detected as a junction; False otherwise.

        Notes:
            - On Python 3.12+, ``os.path.isjunction`` is used if available.
            - On older versions, this function queries the reparse tag via
              Windows APIs (ctypes + ``FSCTL_GET_REPARSE_POINT``).

        """
        try:
            if hasattr(os.path, "isjunction"):
                return os.path.isjunction(str(path))  # type: ignore[attr-defined]

            if not os.path.isdir(str(path)):
                return False

            # IO_REPARSE_TAG_MOUNT_POINT indicates an NTFS junction/mount point.
            IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
            tag = JunctionManager._win_get_reparse_tag(path)
            return tag == IO_REPARSE_TAG_MOUNT_POINT

        except Exception:
            return False

    @staticmethod
    def get_junction_target(path: Path) -> Optional[Path]:
        """Return the target directory of a junction, if readable.

        Args:
            path: Junction path.

        Returns:
            The resolved target :class:`~pathlib.Path` if available; otherwise None.

        """
        try:
            target = os.readlink(str(path))
            return normalize_path(target)
        except (OSError, AttributeError):
            return None

    @staticmethod
    def compare_versions(version1: str, version2: str) -> int:
        """Compare two package version directory strings."""
        return compare_package_versions(version1, version2)

    @staticmethod
    def _temp_junction_path(base: Path, label: str) -> Path:
        suffix = uuid.uuid4().hex[:8]
        return base.with_name(f"{base.name}.{label}.{suffix}")

    @staticmethod
    def _replace_current_junction_atomic(current_path: Path, target_path: Path) -> None:
        if not target_path.exists() or not target_path.is_dir():
            raise RuntimeError(f"Junction target does not exist or is not a directory: {target_path}")

        new_path = JunctionManager._temp_junction_path(current_path, "__new__")
        old_path = JunctionManager._temp_junction_path(current_path, "__old__")
        moved_current = False
        try:
            if os.path.lexists(str(new_path)):
                if JunctionManager.is_junction(new_path):
                    os.rmdir(str(new_path))
                else:
                    raise RuntimeError(f"Temporary junction path already exists and is unsafe to replace: {new_path}")

            if not JunctionManager.create_junction(new_path, target_path):
                raise RuntimeError(f"Failed to create temporary junction at {new_path}")

            new_target = JunctionManager.get_junction_target(new_path)
            if new_target is None or normalize_path(new_target) != normalize_path(target_path):
                raise RuntimeError(f"Temporary junction verification failed: expected {target_path}, got {new_target}")

            if os.path.lexists(str(current_path)):
                os.replace(str(current_path), str(old_path))
                moved_current = True

            os.replace(str(new_path), str(current_path))

            if os.path.lexists(str(old_path)):
                os.rmdir(str(old_path))
        except Exception:
            if not os.path.lexists(str(current_path)) and moved_current and os.path.lexists(str(old_path)):
                try:
                    os.replace(str(old_path), str(current_path))
                except Exception:
                    pass
            raise
        finally:
            if os.path.lexists(str(new_path)):
                try:
                    os.rmdir(str(new_path))
                except OSError:
                    pass
            if os.path.lexists(str(old_path)) and not os.path.lexists(str(current_path)):
                try:
                    os.replace(str(old_path), str(current_path))
                except OSError:
                    pass

    @staticmethod
    def update_current_junction_if_needed(metadata: PackageMetadata, *, force: bool = False) -> bool:
        r"""Update ``<pkg_path>\current`` if the supplied version is not older.

        If ``current`` already exists and points to a newer version, no change is
        made unless ``force=True``.
        """
        current_path = metadata.pkg_path / "current"

        if os.path.lexists(str(current_path)):
            if not JunctionManager.is_junction(current_path):
                raise ValueError(f"{current_path} exists but is not a junction. Aborting all operations.")

            current_target = JunctionManager.get_junction_target(current_path)
            if not current_target:
                raise ValueError(f"{current_path} is a junction but its target is not resolvable. Aborting.")

            current_target = current_target.resolve()
            if not current_target.is_dir():
                print(f"JUNCTION: stale current target detected: {current_target}")
                JunctionManager._replace_current_junction_atomic(current_path, metadata.version_path)
                return True

            if current_target.parent != metadata.pkg_path:
                raise ValueError(
                    f"{current_path} is a junction but its target {current_target} "
                    f"is not under {metadata.pkg_path}. Aborting."
                )

            current_version = current_target.name
            print(f"'current' junction version: {current_version}")
            comparison = JunctionManager.compare_versions(metadata.version_string, current_version)
            if not force and comparison < 0:
                print(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
                return False
            if force:
                print(f"JUNCTION: --force: updating current to {metadata.version_string}")

            JunctionManager._replace_current_junction_atomic(current_path, metadata.version_path)
            return True

        JunctionManager._replace_current_junction_atomic(current_path, metadata.version_path)
        return True



# =============================================================================
# Install steps
# =============================================================================

def _expanded_text_or_error(
    text: str,
    identity: PackageIdentity,
    mode: ExpansionMode,
    *,
    field_label: str,
) -> str:
    """Return expanded text or raise a clear unresolved-variable error."""
    expansion = expand_text(text, identity, mode)
    if expansion.unresolved:
        unresolved = ", ".join(expansion.unresolved)
        raise ValueError(f"{field_label} contains unresolved variable(s): {unresolved}")
    return expansion.value


def prepare_shortcut_spec(
    spec: ShortcutSpec,
    identity: PackageIdentity,
    scope_paths: ScopePaths,
) -> PreparedShortcut:
    """Expand, validate, and materialize a shortcut definition."""
    raw_display_name = spec.name or "<unnamed>"
    expanded_name = _expanded_text_or_error(
        spec.name,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut name for '{raw_display_name}'",
    ).strip()
    expanded_target = _expanded_text_or_error(
        spec.target_path,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut targetPath for '{raw_display_name}'",
    ).strip()
    expanded_arguments = _expanded_text_or_error(
        spec.arguments,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut arguments for '{raw_display_name}'",
    )
    expanded_working_directory = _expanded_text_or_error(
        spec.working_directory,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut workingDirectory for '{raw_display_name}'",
    )
    expanded_icon_location = _expanded_text_or_error(
        spec.icon_location,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut iconLocation for '{raw_display_name}'",
    )
    expanded_description = _expanded_text_or_error(
        spec.description,
        identity,
        ExpansionMode.GENERAL,
        field_label=f"shortcut description for '{raw_display_name}'",
    )

    missing: List[str] = []
    if not expanded_name:
        missing.append("name")
    if not expanded_target:
        missing.append("targetPath")
    if missing:
        raise ValueError(
            f"shortcut '{raw_display_name}' is missing required field(s) after expansion: {', '.join(missing)}"
        )

    scope_paths.shortcut_root.mkdir(parents=True, exist_ok=True)
    shortcut_path = scope_paths.shortcut_root / expanded_name
    if shortcut_path.suffix.lower() != ".lnk":
        shortcut_path = shortcut_path.with_suffix(".lnk")
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)

    return PreparedShortcut(
        name=expanded_name,
        shortcut_path=shortcut_path,
        target_path=expanded_target,
        arguments=expanded_arguments,
        working_directory=expanded_working_directory,
        icon_location=expanded_icon_location,
        description=expanded_description,
    )


class ShortcutInstaller:
    """Create Windows Start Menu shortcuts (.lnk)."""

    @staticmethod
    def _prepare_shortcut(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> PreparedShortcut:
        """Expand and validate shortcut metadata before backend-specific writing."""
        scope_paths = metadata.scope_paths or compute_scope_paths(metadata.scope)
        metadata.scope_paths = scope_paths
        metadata.shortcut_dir = scope_paths.shortcut_root
        spec = ShortcutSpec(
            name=str(shortcut_info.get("name", "") or ""),
            target_path=str(shortcut_info.get("targetPath", "") or ""),
            arguments=str(shortcut_info.get("arguments", "") or ""),
            working_directory=str(shortcut_info.get("workingDirectory", "") or ""),
            icon_location=str(shortcut_info.get("iconLocation", "") or ""),
            description=str(shortcut_info.get("description", "") or ""),
        )
        return prepare_shortcut_spec(spec, metadata.identity, scope_paths)

    @staticmethod
    def _create_shortcut_with_pywin32(
        shortcut_info: Dict[str, str],
        metadata: PackageMetadata,
    ) -> Tuple[bool, Optional[str]]:
        """Create a shortcut using pywin32 (COM)."""
        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_info, metadata)
            win32_client = get_win32com_client()
            if win32_client is None:
                raise RuntimeError("pywin32 is not installed")
            shell = win32_client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(prepared.shortcut_path))
            shortcut.TargetPath = prepared.target_path
            shortcut.Arguments = prepared.arguments
            shortcut.WorkingDirectory = prepared.working_directory
            if prepared.icon_location != "":
                shortcut.IconLocation = prepared.icon_location
            shortcut.Description = prepared.description
            shortcut.Save()
            print(f"SHORTCUT: created: {prepared.shortcut_path.name}")
            return True, None
        except Exception as e:
            name = shortcut_info.get("name", "unknown")
            print(f"SHORTCUT error creating {name}: {e}")
            return False, f"Failed to create shortcut '{name}': {e}"

    @staticmethod
    def _create_shortcut_with_powershell(
        shortcut_info: Dict[str, str],
        metadata: PackageMetadata,
    ) -> Tuple[bool, Optional[str]]:
        """Create a shortcut using PowerShell (fallback)."""
        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_info, metadata)

            def esc(value: str) -> str:
                return value.replace("'", "''")

            shortcut_path_text = esc(str(prepared.shortcut_path))
            target_path = esc(prepared.target_path)
            arguments = esc(prepared.arguments)
            working_dir = esc(prepared.working_directory)
            icon_location = esc(prepared.icon_location)
            description = esc(prepared.description)

            ps_command = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{shortcut_path_text}')
$Shortcut.TargetPath = '{target_path}'
$Shortcut.Arguments = '{arguments}'
$Shortcut.WorkingDirectory = '{working_dir}'
$Shortcut.IconLocation = '{icon_location}'
$Shortcut.Description = '{description}'
$Shortcut.Save()
"""
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_command],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                print(f"SHORTCUT: created (via PowerShell): {prepared.shortcut_path.name}")
                return True, None
            error_text = (result.stderr or result.stdout or "unknown PowerShell shortcut error").strip()
            print(f"SHORTCUT PowerShell error: {error_text}")
            return False, f"Failed to create shortcut '{prepared.name}': {error_text}"
        except Exception as e:
            name = shortcut_info.get("name", "unknown")
            print(f"SHORTCUT error creating {name}: {e}")
            return False, f"Failed to create shortcut '{name}': {e}"

    @staticmethod
    def create_shortcut(
        shortcut_info: Dict[str, str],
        metadata: PackageMetadata,
        backend: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Create a Windows shortcut (.lnk)."""
        backend = backend or get_shortcut_backend()
        if backend == "pywin32":
            return ShortcutInstaller._create_shortcut_with_pywin32(shortcut_info, metadata)
        return ShortcutInstaller._create_shortcut_with_powershell(shortcut_info, metadata)

    @staticmethod
    def install_shortcuts(metadata: PackageMetadata, reporter: Optional[Reporter] = None) -> StepResult:
        """Install all shortcuts defined in ``metadata.shortcut`` and aggregate failures."""
        reporter = reporter or Reporter()
        result = StepResult(ok=True, changed=False)
        if not metadata.shortcut:
            return result

        backend = get_shortcut_backend()
        if backend != "pywin32":
            warning = "pywin32 not available, using PowerShell for shortcut creation"
            reporter.warn(warning)
            result.warnings.append(warning)

        for shortcut_info in metadata.shortcut:
            ok, error = ShortcutInstaller.create_shortcut(shortcut_info, metadata, backend=backend)
            if ok:
                result.changed = True
                continue
            result.ok = False
            message = error or f"Failed to create shortcut: {shortcut_info.get('name', 'unknown')}"
            reporter.error(message)
            result.errors.append(message)

        return result


class EnvironmentVariableManager:
    """Set environment variables in the Windows registry."""

    @staticmethod
    def _get_registry_key(scope: Scope) -> Tuple[int, str]:
        """Return ``(root_hkey, subkey)`` appropriate for the scope."""
        reg = require_winreg()
        if scope == Scope.USER:
            return reg.HKEY_CURRENT_USER, r"Environment"
        return reg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

    @staticmethod
    def broadcast_environment_change() -> None:
        """Broadcast an environment-change notification (WM_SETTINGCHANGE).

        Writing environment values to the registry does not immediately update
        existing processes. Broadcasting improves the odds that new shells and
        some listeners observe updated variables without requiring logoff.

        This is best-effort: failures are reported as warnings.
        """
        try:
            import ctypes
            from ctypes import wintypes

            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002

            SendMessageTimeoutW = ctypes.windll.user32.SendMessageTimeoutW
            SendMessageTimeoutW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(wintypes.ULONG_PTR),
            ]
            SendMessageTimeoutW.restype = wintypes.LPARAM

            result = wintypes.ULONG_PTR(0)
            ok = SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                ctypes.cast(ctypes.c_wchar_p("Environment"), wintypes.LPARAM),
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(result),
            )
            if not ok:
                raise OSError("SendMessageTimeoutW failed")
        except Exception as e:
            print(f"Warning: failed to broadcast environment change notification: {e}")

    @staticmethod
    def set_environment_variable(name: str, value: str, scope: Scope, expand: bool = True) -> bool:
        """Set an environment variable in the registry.

        Args:
            name: Variable name.
            value: Variable value.
            scope: User or Machine scope.
            expand: If True, write as ``REG_EXPAND_SZ`` (so references like
                ``%SystemRoot%`` are expandable); otherwise ``REG_SZ``.

        Returns:
            True on success; False on failure (including permissions errors).

        """
        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            reg = require_winreg()
            with reg.OpenKey(root, subkey, 0, reg.KEY_SET_VALUE) as key:
                reg_type = reg.REG_EXPAND_SZ if expand else reg.REG_SZ
                reg.SetValueEx(key, name, 0, reg_type, value)

            print(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
            EnvironmentVariableManager.broadcast_environment_change()
            return True

        except PermissionError:
            print(f"ERROR: Insufficient permissions to set {scope.value} environment variable: {name}")
            return False
        except Exception as e:
            print(f"ENVIRONMENT error setting {name}: {e}")
            return False

    @staticmethod
    def install_environment_variables(metadata: PackageMetadata, reporter: Optional[Reporter] = None) -> StepResult:
        """Install all environment variables defined in configuration and aggregate failures."""
        reporter = reporter or Reporter()
        result = StepResult(ok=True, changed=False)
        for env_var in metadata.environment:
            name = str(env_var.get("Name", "") or "").strip()
            value = env_var.get("Value", "")
            if not name:
                message = f"Environment variable entry is missing Name: {env_var}"
                reporter.error(message)
                result.ok = False
                result.errors.append(message)
                continue
            expansion = expand_text(str(value), metadata.identity, ExpansionMode.GENERAL)
            if expansion.unresolved:
                unresolved = ", ".join(expansion.unresolved)
                message = f"Environment variable '{name}' contains unresolved variable(s): {unresolved}"
                reporter.error(message)
                result.ok = False
                result.errors.append(message)
                continue
            ok = EnvironmentVariableManager.set_environment_variable(
                name, expansion.value, metadata.scope, expand=True
            )
            if ok:
                result.changed = True
                continue
            message = f"Failed to set environment variable: {name}"
            reporter.error(message)
            result.ok = False
            result.errors.append(message)
        return result


class PATHManager:
    """Read and update the registry-backed PATH value."""

    @staticmethod
    def _path_key(p: str) -> str:
        """Return a normalized comparison key for PATH de-duplication.

        Windows paths are case-insensitive and tolerate mixed separators.
        We normalize for comparisons while preserving original formatting in the
        stored PATH where possible.
        """
        # normpath collapses \ and /; normcase lowercases on Windows.
        key = os.path.normcase(os.path.normpath(p))
        return key.rstrip("\\/")

    @staticmethod
    def get_current_path(scope: Scope) -> List[str]:
        """Read the current PATH entries from the registry.

        Args:
            scope: User or Machine scope.

        Returns:
            A list of PATH entries (strings). Missing/empty PATH returns [].

        """
        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            reg = require_winreg()
            with reg.OpenKey(root, subkey, 0, reg.KEY_READ) as key:
                value, reg_type = reg.QueryValueEx(key, "Path")
                if reg_type in (reg.REG_EXPAND_SZ, reg.REG_SZ):
                    return [p.strip() for p in value.split(";") if p.strip()]
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"PATH error reading {scope.value} PATH: {e}")

        return []

    @staticmethod
    def set_path(path_entries: List[str], scope: Scope) -> bool:
        """Write PATH entries to the registry.

        Args:
            path_entries: List of PATH components.
            scope: User or Machine scope.

        Returns:
            True on success; False on failure.

        """
        try:
            path_value = ";".join(path_entries)
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            reg = require_winreg()
            with reg.OpenKey(root, subkey, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, "Path", 0, reg.REG_EXPAND_SZ, path_value)
            EnvironmentVariableManager.broadcast_environment_change()
            return True
        except PermissionError:
            print(f"ERROR: Insufficient permissions to set {scope.value} PATH")
            return False
        except Exception as e:
            print(f"PATH error setting {scope.value} PATH: {e}")
            return False

    @staticmethod
    def add_to_path(new_entries: List[str], metadata: PackageMetadata, reporter: Optional[Reporter] = None) -> StepResult:
        """Append directories to PATH, avoiding duplicates and surfacing failures."""
        reporter = reporter or Reporter()
        result = StepResult(ok=True, changed=False)
        valid_entries: List[str] = []

        for entry in new_entries:
            expansion = expand_text(str(entry), metadata.identity, ExpansionMode.GENERAL)
            if expansion.unresolved:
                unresolved = ", ".join(expansion.unresolved)
                message = f"PATH entry '{entry}' contains unresolved variable(s): {unresolved}"
                reporter.error(message)
                result.ok = False
                result.errors.append(message)
                continue

            expanded = expansion.value.strip()
            if expanded == "":
                message = f"PATH entry '{entry}' expands to an empty value and will not be added."
                reporter.error(message)
                result.ok = False
                result.errors.append(message)
                continue

            normalized = os.path.normpath(expanded)
            if normalized == "":
                message = f"PATH entry '{entry}' normalized to an empty value and will not be added."
                reporter.error(message)
                result.ok = False
                result.errors.append(message)
                continue

            valid_entries.append(normalized)

        if not valid_entries:
            return result if result.errors else StepResult(ok=True, changed=False)

        current_path = PATHManager.get_current_path(metadata.scope)
        updated_path = current_path.copy()
        existing_keys = {PATHManager._path_key(p) for p in current_path if p}
        added_entries: List[str] = []

        for entry in valid_entries:
            key = PATHManager._path_key(entry)
            if key not in existing_keys:
                updated_path.append(entry)
                existing_keys.add(key)
                added_entries.append(entry)
                reporter.info(f"PATH: adding to {metadata.scope.value} scope: {entry}")

        if not added_entries:
            return result

        if PATHManager.set_path(updated_path, metadata.scope):
            result.changed = True
            return result

        message = f"Failed to update {metadata.scope.value} PATH."
        reporter.error(message)
        result.ok = False
        result.errors.append(message)
        return result

    @staticmethod
    def _system_drive_root() -> str:
        """Return a system drive root like ``C:\\``."""
        system_drive = os.environ.get("SYSTEMDRIVE", "C:")
        if not system_drive.endswith("\\"):
            system_drive += "\\"
        return system_drive

    @staticmethod
    def ensure_bin_in_path(metadata: PackageMetadata, reporter: Optional[Reporter] = None) -> StepResult:
        """Ensure the per-scope bin directory exists and is on PATH."""
        reporter = reporter or Reporter()
        scope_paths = metadata.scope_paths or compute_scope_paths(metadata.scope)
        metadata.scope_paths = scope_paths
        bin_dir = scope_paths.bin_dir

        changed = False
        try:
            existed_before = bin_dir.exists()
            bin_dir.mkdir(parents=True, exist_ok=True)
            changed = not existed_before
        except OSError as e:
            return StepResult(ok=False, errors=[f"Failed to create bin directory {bin_dir}: {e}"])

        current_path = PATHManager.get_current_path(metadata.scope)
        bin_dir_str = str(bin_dir)
        bin_key = PATHManager._path_key(bin_dir_str)
        current_keys = {PATHManager._path_key(p) for p in current_path if p}
        if bin_key not in current_keys:
            path_result = PATHManager.add_to_path([bin_dir_str], metadata, reporter=reporter)
            path_result.changed = path_result.changed or changed
            return path_result

        return StepResult(ok=True, changed=changed)


class BinFileCreator:
    """Create executable wrapper files in the per-scope ``bin`` directory."""

    @staticmethod
    def get_bin_dir(scope: Scope) -> Path:
        """Return the bin directory for a given scope."""
        if scope == Scope.USER:
            return Path.home() / "bin"
        return Path(PATHManager._system_drive_root()) / "bin"

    @staticmethod
    def create_wrapper(wrapper_info: Dict[str, str], metadata: PackageMetadata) -> Tuple[bool, Optional[str]]:
        """Create a wrapper file in the bin directory."""
        try:
            raw_name = str(wrapper_info.get("name", "") or "")
            raw_content = str(wrapper_info.get("content", "") or "")
            if not raw_name:
                raise ValueError("wrapper entry is missing name")

            expanded_name = _expanded_text_or_error(
                raw_name,
                metadata.identity,
                ExpansionMode.GENERAL,
                field_label=f"wrapper name for '{raw_name}'",
            ).strip()
            expanded_content_result = expand_text(raw_content, metadata.identity, ExpansionMode.SCRIPT)
            if expanded_content_result.unresolved:
                unresolved = ", ".join(expanded_content_result.unresolved)
                raise ValueError(
                    f"wrapper '{expanded_name or raw_name}' content contains unresolved variable(s): {unresolved}"
                )
            expanded_content = expanded_content_result.value

            scope_paths = metadata.scope_paths or compute_scope_paths(metadata.scope)
            metadata.scope_paths = scope_paths
            bin_dir = scope_paths.bin_dir
            bin_dir.mkdir(parents=True, exist_ok=True)

            wrapper_path = bin_dir / expanded_name
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)

            ext = wrapper_path.suffix.lower()
            if ext in (".cmd", ".bat"):
                try:
                    desired_bytes = expanded_content.encode("ascii")
                except UnicodeEncodeError:
                    print(
                        f"Warning: non-ASCII content in {ext} wrapper; writing UTF-8 with BOM: {wrapper_path.name}"
                    )
                    desired_bytes = expanded_content.encode("utf-8-sig")
            else:
                desired_bytes = expanded_content.encode("utf-8")

            existed_before = wrapper_path.exists()
            if existed_before:
                try:
                    if wrapper_path.read_bytes() == desired_bytes:
                        print(f"BIN: up-to-date: {wrapper_path}")
                        return True, "unchanged"
                except OSError:
                    pass

            write_bytes_atomic(wrapper_path, desired_bytes)
            action = "updated" if existed_before else "created"
            print(f"BIN: {action}: {wrapper_path}")
            return True, None

        except Exception as e:
            name = wrapper_info.get("name", "unknown")
            print(f"BIN error creating {name}: {e}")
            return False, f"Failed to create wrapper '{name}': {e}"

    @staticmethod
    def install_wrappers(metadata: PackageMetadata, reporter: Optional[Reporter] = None) -> StepResult:
        """Install all wrapper files defined in ``metadata.bin`` and aggregate failures."""
        reporter = reporter or Reporter()
        result = StepResult(ok=True, changed=False)
        for wrapper_info in metadata.bin:
            ok, error = BinFileCreator.create_wrapper(wrapper_info, metadata)
            if ok:
                if error != "unchanged":
                    result.changed = True
                continue
            message = error or f"Failed to create wrapper: {wrapper_info.get('name', 'unknown')}"
            reporter.error(message)
            result.ok = False
            result.errors.append(message)
        return result


def install_shortcuts_step(metadata: PackageMetadata, context: InstallContext) -> StepResult:
    if not metadata.shortcut:
        return StepResult(ok=True, changed=False)
    context.reporter.info("")
    context.reporter.info("Creating shortcuts...")
    return ShortcutInstaller.install_shortcuts(metadata, reporter=context.reporter)


def install_environment_variables_step(metadata: PackageMetadata, context: InstallContext) -> StepResult:
    if not metadata.environment:
        return StepResult(ok=True, changed=False)
    context.reporter.info("")
    context.reporter.info("Setting environment variables...")
    return EnvironmentVariableManager.install_environment_variables(metadata, reporter=context.reporter)


def ensure_bin_in_path_step(metadata: PackageMetadata, context: InstallContext) -> StepResult:
    context.reporter.info("")
    context.reporter.info("Managing PATH...")
    return PATHManager.ensure_bin_in_path(metadata, reporter=context.reporter)


def install_extra_path_entries_step(metadata: PackageMetadata, context: InstallContext) -> StepResult:
    if not metadata.path:
        return StepResult(ok=True, changed=False)
    return PATHManager.add_to_path(metadata.path, metadata, reporter=context.reporter)


def install_wrappers_step(metadata: PackageMetadata, context: InstallContext) -> StepResult:
    if not metadata.bin:
        return StepResult(ok=True, changed=False)
    context.reporter.info("")
    context.reporter.info("Creating executable wrappers...")
    return BinFileCreator.install_wrappers(metadata, reporter=context.reporter)


INSTALL_STEPS = [
    install_shortcuts_step,
    install_environment_variables_step,
    ensure_bin_in_path_step,
    install_extra_path_entries_step,
    install_wrappers_step,
]


# =============================================================================
# Orchestration
# =============================================================================

class PackageManager:
    """Orchestrate install/update operations for a package."""

    def __init__(
        self,
        scope: Scope = Scope.USER,
        pause: bool = False,
        fix_config: bool = False,
        use_defaults: bool = False,
        force: bool = False,
        no_autoupdate_config: bool = False,
    ):
        """Create a :class:`PackageManager`."""
        self.scope = scope
        self.pause = pause
        self.fix_config = fix_config
        self.use_defaults = use_defaults
        self.force = force
        self.reporter = Reporter()

        if no_autoupdate_config:
            self.fix_config = False

    def _is_admin(self) -> bool:
        """Return True if the current process has Administrator privileges."""
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _failure(self, message: str, *, exit_code: int, warnings: Optional[List[str]] = None) -> ActionResult:
        """Create a failed action result after reporting the message."""
        self.reporter.error(message)
        return ActionResult(
            ok=False,
            changed=False,
            warnings=warnings or [],
            errors=[message],
            exit_code=exit_code,
        )

    def install(self, package_path: Path) -> ActionResult:
        """Install a package and return a truthful action result."""
        self.reporter.info("")
        self.reporter.info("=" * 60)
        self.reporter.info("gurlatsev/pkg: Package Manager")
        self.reporter.info(f"Scope: {self.scope.value}")
        self.reporter.info("=" * 60)
        self.reporter.info("")

        try:
            package_path, installing_from_current = self._resolve_install_path(package_path)
        except ValueError as e:
            return self._failure(str(e), exit_code=EXIT_USER_ERROR)

        try:
            metadata = PackageMetadata(package_path)
            metadata.set_scope(self.scope)
            config_data, load_warnings = metadata.load_config(use_defaults=self.use_defaults)
        except DependencyError as e:
            return self._failure(str(e), exit_code=EXIT_USER_ERROR)
        except (ConfigValidationError, RuntimeError, ValueError) as e:
            return self._failure(f"Failed to load package metadata/config: {e}", exit_code=EXIT_USER_ERROR)
        except OSError as e:
            return self._failure(f"Failed to load package metadata/config: {e}", exit_code=EXIT_MUTATION_ERROR)

        warnings = list(load_warnings)
        for warning in load_warnings:
            self.reporter.warn(warning)

        self.reporter.info(f"Package: {metadata.name}")
        self.reporter.info(f"Version: {metadata.version_string}")
        self.reporter.info(f"Path: {metadata.version_path}")
        effective_only_portable = metadata.identity.only_portable_by_name or metadata.only_portable
        self.reporter.info(f"only_portable: {effective_only_portable}")
        self.reporter.info("")

        if effective_only_portable and self.scope == Scope.MACHINE:
            return self._failure(
                "only_portable packages cannot be installed system-wide. Please use User scope.",
                exit_code=EXIT_USER_ERROR,
                warnings=warnings,
            )

        config_sync_changed = False
        inconsistencies = metadata.check_metadata_consistency(config_data)
        if inconsistencies:
            if not self.fix_config:
                self.reporter.error("Configuration inconsistencies detected:")
                for msg in inconsistencies:
                    self.reporter.error(f"  - {msg}")
                self.reporter.info("Aborting installation to avoid mutating configs as a side effect.")
                self.reporter.info("To fix the config, run one of:")
                self.reporter.info(f"  - pkg --action {Action.UPDATE_CONFIG.value} {metadata.version_path}")
                self.reporter.info("  - re-run this install with --fix-config")
                return ActionResult(
                    ok=False,
                    changed=False,
                    warnings=warnings,
                    errors=inconsistencies,
                    exit_code=EXIT_USER_ERROR,
                )

            self.reporter.warn("Configuration inconsistencies detected:")
            for msg in inconsistencies:
                self.reporter.warn(f"  - {msg}")
            self.reporter.info("--fix-config enabled: syncing configuration metadata to match directory structure...")
            try:
                update_result = metadata.update_config(reporter=self.reporter)
            except DependencyError as e:
                return self._failure(str(e), exit_code=EXIT_USER_ERROR, warnings=warnings)
            except (ConfigValidationError, RuntimeError, ValueError) as e:
                return self._failure(f"Failed to update configuration: {e}", exit_code=EXIT_USER_ERROR, warnings=warnings)
            except OSError as e:
                return self._failure(f"Failed to update configuration: {e}", exit_code=EXIT_MUTATION_ERROR, warnings=warnings)
            warnings.extend(update_result.warnings)
            config_sync_changed = update_result.changed
            if not update_result.ok:
                return ActionResult(
                    ok=False,
                    changed=config_sync_changed,
                    warnings=warnings,
                    errors=update_result.errors,
                    exit_code=EXIT_MUTATION_ERROR,
                )
            self.reporter.info("Configuration updated successfully.")
            self.reporter.info("")

        if self.scope == Scope.MACHINE and not self._is_admin():
            return self._failure(
                "Machine scope requires administrator privileges. Please run as administrator.",
                exit_code=EXIT_USER_ERROR,
                warnings=warnings,
            )

        junction_changed = False
        if installing_from_current:
            self.reporter.info("Installing from resolved 'current' target (skipping junction management)")
        else:
            self.reporter.info("Managing 'current' junction...")
            try:
                junction_changed = JunctionManager.update_current_junction_if_needed(metadata, force=self.force)
            except ValueError as e:
                return self._failure(str(e), exit_code=EXIT_USER_ERROR, warnings=warnings)
            except Exception as e:
                return self._failure(str(e), exit_code=EXIT_MUTATION_ERROR, warnings=warnings)

            if not junction_changed and not metadata.is_current:
                self.reporter.info("Skipping component installation (newer version already installed)")
                return ActionResult(
                    ok=True,
                    changed=config_sync_changed,
                    warnings=warnings,
                    exit_code=EXIT_SUCCESS,
                )

        self.reporter.info("")
        self.reporter.info("Installing components...")
        component_result = self._install_components(metadata)
        warnings.extend(component_result.warnings)

        if not component_result.ok:
            self.reporter.error("One or more install steps failed:")
            for error in component_result.errors:
                self.reporter.error(f"  - {error}")
            return ActionResult(
                ok=False,
                changed=junction_changed or config_sync_changed or component_result.changed,
                warnings=warnings,
                errors=component_result.errors,
                exit_code=EXIT_MUTATION_ERROR,
            )

        return ActionResult(
            ok=True,
            changed=junction_changed or config_sync_changed or component_result.changed,
            warnings=warnings,
            exit_code=EXIT_SUCCESS,
        )

    def _resolve_install_path(self, package_path: Path) -> Tuple[Path, bool]:
        """Resolve install input into a version directory path."""
        resolved = resolve_input_path(package_path)
        return resolved.version_path, resolved.installing_from_current


    def _install_components(self, metadata: PackageMetadata) -> StepResult:
        """Install all components declared in config for the given package."""
        context = InstallContext(
            identity=metadata.identity,
            config=metadata.runtime_config or PackageConfig(),
            scope_paths=metadata.scope_paths or compute_scope_paths(metadata.scope),
            reporter=self.reporter,
            force=self.force,
        )
        metadata.scope_paths = context.scope_paths

        results: List[StepResult] = []
        for step in INSTALL_STEPS:
            step_result = step(metadata, context)
            results.append(step_result)

        if not results:
            return StepResult(ok=True, changed=False)

        return combine_step_results(*results)


    def update_config(self, package_path: Path) -> ActionResult:
        """Update ``pkg.toml`` while preserving existing structure when possible."""
        try:
            resolved_path, _ = self._resolve_install_path(package_path)
        except ValueError as e:
            return self._failure(str(e), exit_code=EXIT_USER_ERROR)

        try:
            metadata = PackageMetadata(resolved_path)
            step_result = metadata.update_config(reporter=self.reporter)
        except DependencyError as e:
            return self._failure(str(e), exit_code=EXIT_USER_ERROR)
        except (ConfigValidationError, RuntimeError, ValueError) as e:
            return self._failure(f"Failed to update configuration: {e}", exit_code=EXIT_USER_ERROR)
        except OSError as e:
            return self._failure(f"Failed to update configuration: {e}", exit_code=EXIT_MUTATION_ERROR)

        return ActionResult(
            ok=step_result.ok,
            changed=step_result.changed,
            warnings=step_result.warnings,
            errors=step_result.errors,
            exit_code=EXIT_SUCCESS if step_result.ok else EXIT_MUTATION_ERROR,
        )



class _ExtendedHelpAction(argparse.Action):
    """Argparse action that prints standard help plus :data:`EXTENDED_HELP`."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        print("\n" + EXTENDED_HELP.strip() + "\n")
        parser.exit()


def pause_if_requested(pause: bool) -> None:
    """Pause for a keypress when requested by the CLI."""
    if not pause:
        return
    print()
    print("Press any key to continue...")
    try:
        import msvcrt

        msvcrt.getch()
    except ImportError:
        input("Press Enter to continue...")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv:
            Optional list of arguments (without program name). If None,
            :data:`sys.argv[1:]` is used.

    Returns:
        Process exit code (0 for success, non-zero for failures).

    """
    parser = argparse.ArgumentParser(
        description="Local Package Manager for Windows (gurlatsev/pkg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                         # Install in User scope from current directory\n"
            "  %(prog)s --scope Machine          # Install system-wide (requires admin)\n"
            "  %(prog)s --action UpdateConfig     # Sync configuration metadata only\n"
            "  %(prog)s --fix-config             # Install and sync config metadata if it mismatches\n"
            "  %(prog)s --pause                  # Pause for a keypress before exit\n"
            "  %(prog)s --help-extended          # Show detailed help and config examples\n"
        ),
    )

    parser.add_argument(
        "--help-extended",
        action=_ExtendedHelpAction,
        help="Show standard help plus extended documentation and examples",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )

    parser.add_argument(
        "--python",
        default=None,
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--scope",
        choices=[s.value for s in Scope],
        default=Scope.USER.value,
        help="Installation scope: User (per-user) or Machine (system-wide)",
    )

    parser.add_argument(
        "--action",
        choices=[Action.INSTALL.value, Action.UPDATE_CONFIG.value],
        default=Action.INSTALL.value,
        help="Action to perform",
    )

    parser.add_argument(
        "--pause",
        action="store_true",
        help="Pause for a keypress before exiting",
    )

    parser.add_argument(
        "--no-autoupdate-config",
        action="store_true",
        default=False,
        help="Deprecated (configs are no longer auto-updated by default)",
    )

    parser.add_argument(
        "--fix-config",
        action="store_true",
        default=False,
        help="If config metadata mismatches the directory, sync the owned metadata fields in pkg.toml instead of aborting",
    )

    parser.add_argument(
        "--use-defaults",
        action="store_true",
        default=False,
        help="Proceed with defaults if pkg.toml exists but cannot be parsed/validated",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force install: allow downgrade/reinstall by updating current even if newer is installed",
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to a version directory, current junction, or package root (default: current directory)",
    )

    args = parser.parse_args(argv)
    reporter = Reporter()
    scope = Scope(args.scope)
    action = Action(args.action)
    result: ActionResult

    try:
        manager = PackageManager(
            scope=scope,
            pause=args.pause,
            fix_config=args.fix_config,
            use_defaults=args.use_defaults,
            force=args.force,
            no_autoupdate_config=args.no_autoupdate_config,
        )
        package_path = Path(args.path).expanduser()

        if action == Action.INSTALL:
            result = manager.install(package_path)
        elif action == Action.UPDATE_CONFIG:
            result = manager.update_config(package_path)
        else:
            message = f"Unknown action: {action}"
            reporter.error(message)
            result = ActionResult(ok=False, errors=[message], exit_code=EXIT_USER_ERROR)
    except Exception as e:
        message = f"Unexpected internal error: {e}"
        reporter.error(message)
        result = ActionResult(ok=False, errors=[message], exit_code=EXIT_INTERNAL_ERROR)
    finally:
        pause_if_requested(args.pause)

    print()
    print("-" * 60)
    if result.ok and result.changed:
        print(f"{action.value} completed successfully.")
    elif result.ok:
        print(f"{action.value} completed successfully (no changes needed).")
    else:
        print(f"{action.value} failed.")
    print("-" * 60)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
