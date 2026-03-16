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

The default action is ``Install``. An additional action is available to keep
metadata in sync (``UpdateConfig``).

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

Minimal ``pkg.toml`` snippet:

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
2) Creates Start Menu shortcuts for the packag100e.
3) Sets package-specific environment variables.
4) Ensures a per-scope ``bin`` directory exists and is on ``PATH``.
5) Writes small executable/wrapper files into that ``bin`` directory.

The code is organized as a set of small, single-purpose components coordinated
by :class:`PackageManager`:

- :class:`PackageMetadata`
    Parses the directory naming convention (``v<upstream>.l<local>``) and loads
    the per-version config file (``pkg.toml``). It also computes paths used
    throughout installation.

- :class:`JunctionManager`
    Creates/validates NTFS junctions and compares version strings to decide
    whether ``current`` should be moved to a newer version.

- :class:`VariableExpander`
    Performs variable expansion in configuration strings. It supports:
      * package variables: ``$App``, ``$Icons``, ``$Shortcuts`` (resolved through
        the ``current`` junction), and
      * environment variables: ``$VAR`` or ``${VAR}``.

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

- ``Install`` (default): update ``current`` junction if needed, then install
  shortcuts/env/PATH/bin wrappers.
- ``UpdateConfig``: write a normalized config file reflecting directory-derived
  metadata.


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
import importlib.util
import json
import os
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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


def has_module(module_name: str) -> bool:
    """Return ``True`` when *module_name* is importable."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def get_win32com_client() -> Optional[Any]:
    """Return ``win32com.client`` when available without installing anything."""
    return try_import("win32com.client")


def require_winreg() -> Any:
    """Return the ``winreg`` module or raise a clear platform error."""
    if winreg is None:
        raise OSError("winreg is only available on Windows.")
    return winreg


def load_toml_modules() -> Tuple[Optional[str], Optional[Any]]:
    """Load an available TOML reader without mutating the runtime environment."""
    tomllib_module = try_import("tomllib")
    if tomllib_module is not None:
        return "tomllib", tomllib_module

    toml_module = try_import("toml")
    if toml_module is not None:
        return "toml", toml_module

    return None, None


# =============================================================================
# User-facing documentation, version, and constants
# =============================================================================

__version__ = "0.10.0"
__copyright__ = "Copyright (C) 2025 Gennady Uraltseev. All rights reserved."
__license__ = "MIT"


EXTENDED_HELP = r"""
Extended help
-------------

Quick start
~~~~~~~~~~~

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
   - ``$App`` is expanded by pkg before writing the file.

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

- Package variables are expanded first:
    $App, $Icons, $Shortcuts
  These resolve through the stable ``current`` junction path (e.g.
  ``<pkg>\\current\\App``). The junction does not need to exist for simple
  string substitution.

- Environment variables are expanded next:
    $VAR and ${VAR}

- Escaping:
    Use ``$$`` to produce a literal ``$`` (for example, ``$$App`` becomes
    the literal text ``$App`` rather than expanding to a path).

Notes
~~~~~

- After registry PATH changes, existing terminals won't automatically see the
  new PATH. Open a new terminal or log off/on.
- TOML support:
    - Python 3.11+ uses the stdlib ``tomllib`` module (no pip required).
    - On older Python versions, install the third-party ``toml`` package:
        pip install toml
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


PYWIN32_AVAILABLE = has_module("win32com.client")
TOML_BACKEND, _TOML_MODULE = load_toml_modules()
TOML_AVAILABLE = TOML_BACKEND is not None



# =============================================================================
# Enums, dataclasses, and custom exceptions
# =============================================================================

class ConfigValidationError(ValueError):
    """Raised when pkg.toml configuration is invalid."""


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
    COMPRESS = "Compress"


# =============================================================================
# Package identity and scope paths
# =============================================================================

# Existing PackageMetadata remains the compatibility facade in this phase.

# =============================================================================
# Runtime config model and validation
# =============================================================================

class PackageMetadata:
    """Package metadata derived from the filesystem and configuration.

    Instances are created from a *version directory* (``v<upstream>.l<local>``)
    or from a ``current`` junction that points to such a directory.

    Public attributes are populated from:
      1) directory structure, and
      2) configuration file (TOML).

    """

    def __init__(self, version_path: Path):
        """Initialize package metadata.

        Args:
            version_path:
                Either the path to a version directory (e.g. ``...\v1.0.0.l1``)
                or the path to the ``current`` junction inside the package root.

        Raises:
            ValueError: If the path does not represent a valid version directory
                (directly or via ``current``).
        """
        self.input_path = Path(version_path)

        # Resolve "current" junction inputs to the actual version directory.
        if self.input_path.name.lower() == "current" and JunctionManager.is_junction(self.input_path):
            target = JunctionManager.get_junction_target(self.input_path)
            if not target:
                raise ValueError(f'"current" junction target is not resolvable: {self.input_path}')
            self.version_path = target
            self.pkg_path = self.input_path.parent
        else:
            self.version_path = self.input_path
            self.pkg_path = self.version_path.parent

        self.name = self.pkg_path.name
        self.version: str = ""
        self.local_version: str = ""
        self.version_string: str = ""
        self.is_current: bool = False
        self.scope: Scope = Scope.USER
        self.shortcut_dir: Path = Path()
        self.only_portable: bool = False

        # Fields from configuration (optional)
        self.description: Optional[str] = None
        self.homepage: Optional[str] = None
        self.download_url: Optional[str] = None
        self.environment: List[Dict[str, str]] = []
        self.bin: List[Dict[str, str]] = []
        self.path: List[str] = []
        self.shortcut: List[Dict[str, str]] = []

        self._fill_from_directory()
        self._fill_current()

    def _fill_from_directory(self) -> None:
        """Extract package metadata from directory structure.

        Expected version directory name format: ``v<upstream>.l<local>``.

        Populates:
          - ``version`` (upstream)
          - ``local_version`` (local revision)
          - ``version_string`` (full directory name)
          - ``only_portable`` (inferred from package name suffix ``-portable``)

        Raises:
            ValueError: If the version directory does not match the naming scheme.
        """
        version_dir_name = str(self.version_path.relative_to(self.pkg_path))

        match = VERSION_DIR_NAME_RE.match(version_dir_name)
        if not match:
            raise ValueError(
                f"Invalid version directory name: {version_dir_name}. "
                "Expected format: v<upstream>.l<local>"
            )

        self.version = match.group(1)  # Upstream version
        self.local_version = match.group(2)  # Local revision
        self.version_string = version_dir_name

        if self.name.lower().endswith("-portable"):
            self.only_portable = True


    def _fill_current(self) -> None:
        r"""Compute whether this version is the package's current version.

        The package is considered "current" if ``<pkg_path>\current`` exists
        and is a junction pointing to this instance's ``version_path``.
        """
        current_path = self.pkg_path / "current"
        if not (current_path.exists() and JunctionManager.is_junction(current_path)):
            return

        try:
            target = JunctionManager.get_junction_target(current_path)
            if target:
                self.is_current = target.resolve() == self.version_path.resolve()
        except OSError:
            # If the target can't be read, treat as not current.
            self.is_current = False

    def check_metadata_consistency(self, config_data: Dict) -> List[str]:
        """Check consistency between directory-derived and config-derived metadata.

        This is used primarily to detect cases where the config file has stale
        values for ``name``, ``version``, ``localVersion``, or portability flags.

        Args:
            config_data: Parsed configuration dict (from TOML).

        Returns:
            A list of human-readable inconsistency messages. An empty list means
            the config is consistent with the directory structure.

        """
        inconsistencies: List[str] = []

        config_name = config_data.get("name", "")
        if config_name and config_name != self.name:
            inconsistencies.append(f"Name mismatch: directory='{self.name}', config='{config_name}'")

        config_version = config_data.get("version", "")
        if config_version and config_version != self.version:
            inconsistencies.append(
                f"Version mismatch: directory='{self.version}', config='{config_version}'"
            )

        config_local_version = config_data.get("localVersion", "")
        if config_local_version and str(config_local_version) != self.local_version:
            inconsistencies.append(
                f"LocalVersion mismatch: directory='{self.local_version}', config='{config_local_version}'"
            )

        # Support both 'only_portable' (current) and 'portable' (legacy)
        cfg_only_portable = config_data.get("only_portable", None)
        if cfg_only_portable is None and "portable" in config_data:
            cfg_only_portable = bool(config_data["portable"])

        if cfg_only_portable is not None and bool(cfg_only_portable) != self.only_portable:
            inconsistencies.append(
                f"Portable flag mismatch: directory='{self.only_portable}', config='{bool(cfg_only_portable)}'"
            )

        return inconsistencies


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
        """Render a scalar/list value as TOML literal text."""
        if value is None:
            return '""'
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(PackageMetadata._to_toml_scalar(v) for v in value) + "]"
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _toml_path_lines(path_entries: List[str]) -> List[str]:
        """Render ``path`` as repeated ``[[path]]`` TOML tables."""
        if not path_entries:
            return []

        lines: List[str] = []
        for entry in path_entries:
            lines.extend([
                "[[path]]",
                f"value = {PackageMetadata._to_toml_scalar(entry)}",
                "",
            ])
        return lines

    def _write_pkg_toml(self, toml_path: Path, data: Dict[str, Any], preface: Optional[List[str]] = None) -> None:
        """Write a normalized ``pkg.toml`` with canonical table/list formatting."""
        lines: List[str] = []
        if preface:
            lines.extend(preface)

        lines.extend([
            "[[main]]",
            f"name = {self._to_toml_scalar(data.get('name', self.name))}",
            f"version = {self._to_toml_scalar(data.get('version', self.version))}",
            f"localVersion = {self._to_toml_scalar(data.get('localVersion', self.local_version))}",
            f"only_portable = {self._to_toml_scalar(data.get('only_portable', self.only_portable))}",
            f"description = {self._to_toml_scalar(data.get('description'))}",
            f"homepage = {self._to_toml_scalar(data.get('homepage'))}",
            f"downloadURL = {self._to_toml_scalar(data.get('downloadURL'))}",
        ])

        lines.append("")
        lines.extend(self._toml_path_lines(data.get("path", [])))
        lines.append("")

        for ev in data.get("environment", []):
            lines.extend([
                "[[environment]]",
                f"Name = {self._to_toml_scalar(ev.get('Name', ''))}",
                f"Value = {self._to_toml_scalar(ev.get('Value', ''))}",
                "",
            ])

        for sc in data.get("shortcut", []):
            lines.append("[[shortcut]]")
            for key in ["name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description"]:
                if key in sc and sc.get(key) not in (None, ""):
                    lines.append(f"{key} = {self._to_toml_scalar(sc.get(key))}")
            lines.append("")

        for bw in data.get("bin", []):
            lines.extend([
                "[[bin]]",
                f"name = {self._to_toml_scalar(bw.get('name', ''))}",
                f"content = {self._to_toml_scalar(bw.get('content', ''))}",
                "",
            ])

        toml_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_best_guess_toml(self, data: Dict[str, Any]) -> None:
        """Write a best-guess ``pkg.toml`` with commented examples.

        This is used when ``pkg.toml`` is missing. Existing/default metadata is
        used as the source, and commented example sections are included for
        ``shortcut`` and ``bin`` entries.
        """
        toml_path = self.version_path / "pkg.toml"
        data = self._canonicalize_config_dict(data)
        self._validate_config_dict(data)

        lines: List[str] = [
            "# Auto-generated by pkg (best guess).",
            "# You can edit this file manually.",
            "# Example main section:",
            "# [[main]]",
            '# name = "MyApp"',
            '# version = "1.2.3"',
            "# localVersion = 1",
            "",
            "# Example PATH entry (table form):",
            '# [[path]]',
            '# value = "$App"',
            '# [[path]]',
            '# value = "$App\\bin"',
            "",
            "# Example shortcut entry:",
            '# [[shortcut]]',
            '# name = "My App"',
            "# targetPath = \"$App/MyApp.exe\"",
            '# workingDirectory = "$App"',
            "# iconLocation = \"$Icons/myapp.ico,0\"",
            '# description = "Launch My App"',
            "",
            "# Example bin entry:",
            '# $App, $Icons, $Shortcuts gets replaced with the package App directory at install time',
            '# [[bin]]',
            '# name = "myapp.cmd"',
            '# content = """',
            "# @echo off",
            '# @ $App\\app.exe %*',
            '# """',
            "",
        ]
        self._write_pkg_toml(toml_path, data, preface=lines)
        print(f"Generated: {toml_path} (best guess)")

    def load_config(self, *, use_defaults: bool = False) -> Dict:
        """Load configuration from ``pkg.toml``.

        Behavior goals:

        - If a config file exists but cannot be parsed/validated, fail fast.
          Installing with silent defaults is risky because it can skip expected
          shortcuts/env/PATH/bin actions.
        - The only exception is when the caller explicitly opts in via
          ``use_defaults=True`` (exposed as ``--use-defaults`` on the CLI).

        Returns:
            The parsed configuration dictionary (or defaults if no file exists).

        Raises:
            RuntimeError: If ``pkg.toml`` exists but cannot be loaded/validated
                and ``use_defaults`` is False.
        """
        default_data: Dict = {
            "name": self.name,
            "version": self.version,
            "localVersion": self.local_version,
            "description": None,
            "homepage": None,
            "downloadURL": None,
            "environment": [],
            "bin": [],
            "path": [],
            "shortcut": [],
            "only_portable": self.only_portable,
        }

        toml_path = self.version_path / "pkg.toml"

        if toml_path.exists():
            if not TOML_AVAILABLE:
                msg_lines = [
                    "pkg.toml found but no TOML parser is available for this Python interpreter.",
                ]
                if importlib.util.find_spec("pip") is None:
                    msg_lines.append(
                        "This Python interpreter does not include pip, so pkg cannot auto-install the 'toml' package."
                    )
                msg_lines.extend(
                    [
                        "To enable TOML support, either:",
                        "  - run pkg with Python 3.11+ (stdlib tomllib), or",
                        "  - install the third-party 'toml' package (pip install toml).",
                    ]
                )
                msg = "\n".join(msg_lines)
                if not use_defaults:
                    raise RuntimeError(msg)
                print(f"WARNING: {msg}")
                print("WARNING: Proceeding with defaults because --use-defaults was provided.")
                data = default_data
            else:
                try:
                    if TOML_BACKEND == "tomllib":
                        with open(toml_path, "rb") as f:
                            data = _TOML_MODULE.load(f)  # type: ignore[union-attr]
                    else:
                        with open(toml_path, "r", encoding="utf-8") as f:
                            data = _TOML_MODULE.load(f)  # type: ignore[union-attr]
                except Exception as e:
                    if not use_defaults:
                        raise RuntimeError(f"Error loading TOML config from {toml_path}: {e}") from e
                    print(f"WARNING: Error loading TOML config from {toml_path}: {e}")
                    print("WARNING: Proceeding with defaults because --use-defaults was provided.")
                    data = default_data

            # Canonicalize + validate (even for defaults), then load.
            data = self._canonicalize_config_dict(data)
            self._validate_config_dict(data)
            self._load_from_dict(data)
            return data

        # No config file: use defaults and (optionally) generate a starter TOML.
        default_data = self._canonicalize_config_dict(default_data)
        self._validate_config_dict(default_data)
        self._load_from_dict(default_data)

        # Keep the prior "best guess" behavior for first-time packages.
        # This is only done when no pkg.toml exists.
        if TOML_AVAILABLE and not toml_path.exists():
            self._write_best_guess_toml(default_data)
        return default_data

    def _load_from_dict(self, data: Dict) -> None:
        """Populate instance fields from a configuration dictionary.

        This method is intentionally permissive: missing keys leave the existing
        value unchanged, and it supports both legacy ``portable`` and current
        ``only_portable`` flags.
        """
        self.description = data.get("description", self.description)
        self.homepage = data.get("homepage", self.homepage)
        self.download_url = data.get("downloadURL", self.download_url)
        self.environment = data.get("environment", self.environment)
        self.bin = data.get("bin", self.bin)
        self.path = data.get("path", self.path) or []
        self.shortcut = data.get("shortcut", self.shortcut)

        if "only_portable" in data:
            self.only_portable = bool(data["only_portable"])
        elif "portable" in data:
            self.only_portable = bool(data["portable"])
        elif self.name.lower().endswith("-portable"):
            self.only_portable = True

    def update_config(self, data: Optional[Dict] = None) -> None:
        """Write metadata back to configuration file.

        By default, this writes a normalized config that reflects the current
        :class:`PackageMetadata` fields.

        Args:
            data:
                Optional base dictionary to write. If provided, directory-derived
                fields (name/version/localVersion/only_portable) are overwritten
                to match the filesystem.

        Returns:
            None. Writes a file to disk and prints a status message.

        """
        if data is None:
            data = {
                "name": self.name,
                "version": self.version,
                "localVersion": self.local_version,
                "description": self.description,
                "homepage": self.homepage,
                "downloadURL": self.download_url,
                "environment": self.environment,
                "bin": self.bin,
                "path": self.path,
                "shortcut": self.shortcut,
                "only_portable": self.only_portable,
            }

        else:
            data = self._canonicalize_config_dict(data)
            self._validate_config_dict(data)
            data["name"] = self.name
            data["version"] = self.version
            data["localVersion"] = self.local_version
            data["only_portable"] = self.only_portable

        # Final sanity validation before writing.
        self._validate_config_dict(data)

        if self.name.lower().endswith("-portable") and not self.only_portable:
            self.only_portable = True
            data["only_portable"] = True

        toml_path = self.version_path / "pkg.toml"
        json_path = self.version_path / "pkg.json"  # legacy; removed if present

        self._write_pkg_toml(toml_path, data)
        print(f"Updated: {toml_path}")

        if json_path.exists():
            try:
                json_path.unlink()
                print(f"Removed: {json_path} (legacy config; TOML-only)")
            except OSError as e:
                print(f"Warning: failed to remove legacy JSON config {json_path}: {e}")

    def set_scope(self, scope: Scope) -> None:
        """Set installation scope and compute Start Menu target directory.

        Args:
            scope: The installation scope.

        Returns:
            None. Updates ``self.scope`` and ``self.shortcut_dir`` and ensures the
            directory exists.

        """
        self.scope = scope

        if scope == Scope.USER:
            appdata = os.environ.get("APPDATA", "")
            self.shortcut_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "opt"
        else:
            programdata = os.environ.get("PROGRAMDATA", "")
            self.shortcut_dir = (
                Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "opt"
            )

        self.shortcut_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# TOML document I/O and round-trip config updates
# =============================================================================

# Existing config file I/O remains dict-based in this phase.

# =============================================================================
# Variable expansion
# =============================================================================

class VariableExpander:
    """Expand $-style variables in config strings."""

    @staticmethod
    def expand_variables(text: str, metadata: PackageMetadata) -> str:
        """Expand variables in a string using ``$`` syntax.

        Expansion order:

        1) Package variables: ``$App``, ``$Icons``, ``$Shortcuts``.
           These resolve through the package's stable ``current`` junction path
           (for example: ``<pkg>\\current\\App``).
           The junction does not need to exist for string substitution.
        2) Environment variables: ``${VAR}`` then ``$VAR``.

        Escaping:
          - ``$$`` produces a literal ``$``. For example, ``$$App`` becomes the
            literal text ``$App`` (it will not be expanded).

        Args:
            text: Input string containing variables.
            metadata: Package metadata (used to resolve package variables).

        Returns:
            The expanded string.

        """
        if not text:
            return text

        # Preserve user-intended literal dollars.
        # We only unescape the dollars that were present in the input (not ones
        # that might appear via environment-variable expansion).
        sentinel = "\x00PKG_DOLLAR\x00"
        text = text.replace("$$", sentinel)

        # Package vars: boundary-safe ($App does not match $AppData).
        pkg_base = metadata.pkg_path / "current"
        pkg_map = {
            "App": str(pkg_base / "App"),
            "Icons": str(pkg_base / "Icons"),
            "Shortcuts": str(pkg_base / "Shortcuts"),
        }

        def repl_pkg(match: re.Match) -> str:
            name = match.group(1)
            return pkg_map.get(name, match.group(0))

        text = re.sub(r"\$(App|Icons|Shortcuts)\b", repl_pkg, text)

        # ${VAR} environment expansion.
        def repl_env_braces(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")

        text = re.sub(r"\$\{([^}]+)\}", repl_env_braces, text)

        # $VAR environment expansion.
        # Variable names here intentionally follow shell-like ASCII word rules.
        def repl_env_plain(match: re.Match) -> str:
            var_name = match.group(1)
            # Avoid surprising double-expansion if someone happens to have an
            # environment variable named App/Icons/Shortcuts.
            if var_name in pkg_map:
                return match.group(0)
            return os.environ.get(var_name, "")

        text = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", repl_env_plain, text)

        # Restore escaped dollars.
        return text.replace(sentinel, "$")

    @staticmethod
    def expand_dict(data: Dict[str, str], metadata: PackageMetadata) -> Dict[str, str]:
        """Expand variables in all values of a dictionary.

        Args:
            data: Mapping from keys to template strings.
            metadata: Package metadata for package-variable expansion.

        Returns:
            A new dict where each value has been expanded using
            :meth:`expand_variables`.

        """
        return {key: VariableExpander.expand_variables(value, metadata) for key, value in data.items()}


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
                creationflags=subprocess.CREATE_NO_WINDOW,
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
    def update_current_junction_if_needed(metadata: PackageMetadata, *, force: bool = False) -> bool:
        r"""Update ``<pkg_path>\current`` if the supplied version is not older.

        If ``current`` already exists and points to a newer version, no change is
        made unless ``force=True``.

        Args:
            metadata: Package metadata for the candidate version.
            force: If True, update ``current`` even when it would be a downgrade.

        Returns:
            True if ``current`` was created/updated; False if it was left unchanged.

        Raises:
            ValueError: If ``current`` exists but is not a junction or points
                somewhere unexpected.
            RuntimeError: If creating or updating the junction fails.

        """
        current_path = metadata.pkg_path / "current"

        if os.path.lexists(str(current_path)):
            if not JunctionManager.is_junction(current_path):
                raise ValueError(
                    f"{current_path} exists but is not a junction. Aborting all operations."
                )

            current_target = JunctionManager.get_junction_target(current_path)
            if not current_target:
                raise ValueError(
                    f"{current_path} is a junction but its target is not resolvable. Aborting."
                )

            current_target = current_target.resolve()
            if not current_target.is_dir():
                print(
                    f"JUNCTION: removing stale 'current' target (missing directory): {current_target}"
                )
                if JunctionManager.create_junction(current_path, metadata.version_path):
                    return True
                raise RuntimeError(f"Failed to update 'current' junction at {current_path}")

            if current_target.parent != metadata.pkg_path:
                raise ValueError(
                    f"{current_path} is a junction but its target {current_target} "
                    f"is not under {metadata.pkg_path}. Aborting."
                )

            current_version = current_target.name
            print(f"'current' junction version: {current_version}")
            comparison = JunctionManager.compare_versions(metadata.version_string, current_version)

            if force:
                print(f"JUNCTION: --force: updating current to {metadata.version_string}")
                if JunctionManager.create_junction(current_path, metadata.version_path):
                    return True
                raise RuntimeError(f"Failed to update 'current' junction at {current_path}")

            if comparison >= 0:
                if JunctionManager.create_junction(current_path, metadata.version_path):
                    return True
                raise RuntimeError(f"Failed to update 'current' junction at {current_path}")

            print(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
            return False

        if JunctionManager.create_junction(current_path, metadata.version_path):
            return True
        raise RuntimeError(f"Failed to create 'current' junction at {current_path}")


# =============================================================================
# Install steps
# =============================================================================

class ShortcutInstaller:
    """Create Windows Start Menu shortcuts (.lnk)."""

    @staticmethod
    def _create_shortcut_with_pywin32(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create a shortcut using pywin32 (COM)."""
        try:
            expanded = VariableExpander.expand_dict(shortcut_info, metadata)
            name = str(expanded.get("name", "") or "").strip()
            target_required = str(expanded.get("targetPath", "") or "").strip()
            if not name or not target_required:
                missing = []
                if not name:
                    missing.append("name")
                if not target_required:
                    missing.append("targetPath")
                print(f"SHORTCUT error: missing required key(s) {missing} in entry: {shortcut_info}")
                return False

            metadata.shortcut_dir.mkdir(parents=True, exist_ok=True)

            shortcut_path = metadata.shortcut_dir / name
            if shortcut_path.suffix.lower() != ".lnk":
                shortcut_path = shortcut_path.with_suffix(".lnk")

            # If the shortcut name includes subfolders (e.g. "Tools\My App"),
            # ensure the parent directory exists before creating the .lnk.
            shortcut_path.parent.mkdir(parents=True, exist_ok=True)

            win32_client = get_win32com_client()
            if win32_client is None:
                raise RuntimeError("pywin32 is not installed")
            shell = win32_client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(shortcut_path))

            shortcut.TargetPath = target_required
            if "arguments" in expanded:
                shortcut.Arguments = expanded["arguments"]
            if "workingDirectory" in expanded:
                shortcut.WorkingDirectory = expanded["workingDirectory"]
            if "iconLocation" in expanded and expanded["iconLocation"] != "":
                shortcut.IconLocation = expanded["iconLocation"]
            if "description" in expanded:
                shortcut.Description = expanded["description"]

            shortcut.Save()
            print(f"SHORTCUT: created: {shortcut_path.name}")
            return True

        except Exception as e:
            print(f"SHORTCUT error creating {shortcut_info.get('name', 'unknown')}: {e}")
            return False

    @staticmethod
    def _create_shortcut_with_powershell(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create a shortcut using PowerShell (fallback)."""
        try:
            expanded = VariableExpander.expand_dict(shortcut_info, metadata)
            name = str(expanded.get("name", "") or "").strip()
            target_required = str(expanded.get("targetPath", "") or "").strip()
            if not name or not target_required:
                missing = []
                if not name:
                    missing.append("name")
                if not target_required:
                    missing.append("targetPath")
                print(f"SHORTCUT error: missing required key(s) {missing} in entry: {shortcut_info}")
                return False

            metadata.shortcut_dir.mkdir(parents=True, exist_ok=True)

            shortcut_path = metadata.shortcut_dir / name
            if shortcut_path.suffix.lower() != ".lnk":
                shortcut_path = shortcut_path.with_suffix(".lnk")

            # If the shortcut name includes subfolders (e.g. "Tools\My App"),
            # ensure the parent directory exists before creating the .lnk.
            shortcut_path.parent.mkdir(parents=True, exist_ok=True)

            def esc(s: str) -> str:
                return s.replace("'", "''")

            target_path = esc(target_required)
            arguments = esc(expanded.get("arguments", ""))
            working_dir = esc(expanded.get("workingDirectory", ""))
            icon_location = esc(expanded.get("iconLocation", ""))
            description = esc(expanded.get("description", ""))

            ps_command = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{shortcut_path}')
$Shortcut.TargetPath = '{target_path}'
$Shortcut.Arguments = '{arguments}'
$Shortcut.WorkingDirectory = '{working_dir}'
$Shortcut.IconLocation = '{icon_location}'
$Shortcut.Description = '{description}'
$Shortcut.Save()
"""

            result = subprocess.run(
                ["powershell", "-Command", ps_command],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if result.returncode == 0:
                print(f"SHORTCUT: created (via PowerShell): {shortcut_path.name}")
                return True

            print(f"SHORTCUT PowerShell error: {result.stderr}")
            return False

        except Exception as e:
            print(f"SHORTCUT error creating {shortcut_info.get('name', 'unknown')}: {e}")
            return False

    @staticmethod
    def create_shortcut(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create a Windows shortcut (.lnk).

        Args:
            shortcut_info: Shortcut definition dict (see extended help).
            metadata: Package metadata for variable expansion and scope paths.

        Returns:
            True if the shortcut was created successfully, False otherwise.

        """
        if PYWIN32_AVAILABLE:
            return ShortcutInstaller._create_shortcut_with_pywin32(shortcut_info, metadata)

        print("Warning: pywin32 not available, using PowerShell for shortcut creation")
        return ShortcutInstaller._create_shortcut_with_powershell(shortcut_info, metadata)

    @staticmethod
    def install_shortcuts(metadata: PackageMetadata) -> None:
        """Install all shortcuts defined in ``metadata.shortcut``.

        Args:
            metadata: Package metadata with loaded config.

        Returns:
            None.

        """
        if not PYWIN32_AVAILABLE:
            print("Note: Using PowerShell for shortcut creation (pywin32 not available)")

        for shortcut_info in metadata.shortcut:
            ShortcutInstaller.create_shortcut(shortcut_info, metadata)


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
                wintypes.LPCWSTR,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(wintypes.DWORD),
            ]
            SendMessageTimeoutW.restype = wintypes.LPARAM

            result = wintypes.DWORD(0)
            SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(result),
            )
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
    def install_environment_variables(metadata: PackageMetadata) -> None:
        """Install all environment variables defined in configuration.

        Each environment variable entry must be a dict with keys ``Name`` and
        ``Value`` (note the capitalization).

        Args:
            metadata: Package metadata.

        Returns:
            None.

        """
        for env_var in metadata.environment:
            name = env_var.get("Name", "")
            value = env_var.get("Value", "")
            if name:
                expanded_value = VariableExpander.expand_variables(value, metadata)
                EnvironmentVariableManager.set_environment_variable(
                    name, expanded_value, metadata.scope, expand=True
                )


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
    def add_to_path(new_entries: List[str], metadata: PackageMetadata) -> bool:
        """Append directories to PATH, avoiding duplicates.

        Args:
            new_entries: List of directory strings to append. Entries may use
                variables (see :class:`VariableExpander`).
            metadata: Package metadata for variable expansion and scope.

        Returns:
            True if PATH is successfully updated (or already contained the entries);
            False if writing the registry value failed.

        """
        expanded_new_entries: List[str] = []
        for entry in new_entries:
            expanded = VariableExpander.expand_variables(entry, metadata)
            expanded_new_entries.append(os.path.normpath(expanded))

        current_path = PATHManager.get_current_path(metadata.scope)
        updated_path = current_path.copy()

        existing_keys = {PATHManager._path_key(p) for p in current_path if p}

        for entry in expanded_new_entries:
            if not entry:
                continue

            key = PATHManager._path_key(entry)
            if key not in existing_keys:
                updated_path.append(entry)
                existing_keys.add(key)
                print(f"PATH: adding to {metadata.scope.value} scope: {entry}")

        if current_path != updated_path:
            return PATHManager.set_path(updated_path, metadata.scope)

        return True

    @staticmethod
    def _system_drive_root() -> str:
        """Return a system drive root like ``C:\\``."""
        system_drive = os.environ.get("SYSTEMDRIVE", "C:")
        if not system_drive.endswith("\\"):
            system_drive += "\\"
        return system_drive

    @staticmethod
    def ensure_bin_in_path(metadata: PackageMetadata) -> bool:
        """Ensure the per-scope bin directory exists and is on PATH.

        User bin dir: ``%USERPROFILE%\bin``.
        Machine bin dir: ``<SYSTEMDRIVE>\bin``.

        Args:
            metadata: Package metadata (scope determines which bin dir is used).

        Returns:
            True if the bin directory exists and is on PATH; False on registry write
            failure.

        """
        if metadata.scope == Scope.USER:
            bin_dir = Path.home() / "bin"
        else:
            bin_dir = Path(PATHManager._system_drive_root()) / "bin"

        bin_dir.mkdir(parents=True, exist_ok=True)

        current_path = PATHManager.get_current_path(metadata.scope)
        bin_dir_str = str(bin_dir)

        bin_key = PATHManager._path_key(bin_dir_str)
        current_keys = {PATHManager._path_key(p) for p in current_path if p}
        if bin_key not in current_keys:
            return PATHManager.add_to_path([bin_dir_str], metadata)

        return True


class BinFileCreator:
    """Create executable wrapper files in the per-scope ``bin`` directory."""

    @staticmethod
    def get_bin_dir(scope: Scope) -> Path:
        """Return the bin directory for a given scope.

        Args:
            scope: User or Machine scope.

        Returns:
            A :class:`~pathlib.Path` to the bin directory.

        """
        if scope == Scope.USER:
            return Path.home() / "bin"
        return Path(PATHManager._system_drive_root()) / "bin"

    @staticmethod
    def create_wrapper(wrapper_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create a wrapper file in the bin directory.

        Args:
            wrapper_info:
                Wrapper definition dict with keys:
                - ``name`` (required): output file name
                - ``content`` (required): file contents (after variable expansion)
            metadata: Package metadata for variable expansion and scope.

        Returns:
            True if the wrapper was written successfully; False otherwise.

        """
        try:
            name = wrapper_info.get("name", "")
            content = wrapper_info.get("content", "")
            if not name:
                return False

            expanded_content = VariableExpander.expand_variables(content, metadata)

            bin_dir = BinFileCreator.get_bin_dir(metadata.scope)
            bin_dir.mkdir(parents=True, exist_ok=True)

            wrapper_path = bin_dir / name
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)

            ext = wrapper_path.suffix.lower()
            # cmd.exe historically uses legacy code pages; UTF-8 can be
            # problematic for non-ASCII. Prefer ASCII when possible.
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
                        return True
                except OSError:
                    # Fall through to rewrite.
                    pass

            wrapper_path.write_bytes(desired_bytes)
            action = "updated" if existed_before else "created"
            print(f"BIN: {action}: {wrapper_path}")
            return True

        except Exception as e:
            print(f"BIN error creating {wrapper_info.get('name', 'unknown')}: {e}")
            return False

    @staticmethod
    def install_wrappers(metadata: PackageMetadata) -> None:
        """Install all wrapper files defined in ``metadata.bin``.

        Args:
            metadata: Package metadata.

        Returns:
            None.

        """
        for wrapper_info in metadata.bin:
            BinFileCreator.create_wrapper(wrapper_info, metadata)


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
        """Create a :class:`PackageManager`.

        Args:
            scope: Installation scope.
            pause: If True, pause for a keypress before exiting (useful in double-click scenarios).
            fix_config:
                If True, rewrite ``pkg.toml`` when config mismatches directory metadata.
                If False (default), abort with a clear error.
            use_defaults:
                If True, proceed with defaults if ``pkg.toml`` exists but cannot
                be parsed/validated.
            force:
                If True, allow downgrades/reinstalls by updating ``current`` even
                when it points to a newer version.
            no_autoupdate_config:
                Deprecated alias retained for backwards compatibility.

        Raises:
            SystemExit: If Machine scope is requested without Administrator privileges.

        """
        self.scope = scope
        self.pause = pause
        self.fix_config = fix_config
        self.use_defaults = use_defaults
        self.force = force

        # Deprecated: previous default was to auto-update config unless this
        # flag was set. New default is to abort unless fix_config=True.
        if no_autoupdate_config:
            self.fix_config = False

        if not PYWIN32_AVAILABLE:
            print("Warning: pywin32 not available. Shortcuts will be created using PowerShell.")
            print("For better performance, install pywin32: pip install pywin32")

        if scope == Scope.MACHINE and not self._is_admin():
            print("ERROR: Machine scope requires administrator privileges.")
            print("Please run as administrator.")
            sys.exit(1)

    def _is_admin(self) -> bool:
        """Return True if the current process has Administrator privileges."""
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def install(self, package_path: Path) -> None:
        """Install a package.

        Args:
            package_path:
                Path to a version directory (``v<upstream>.l<local>``) or the
                ``current`` junction, or the package root (containing ``current``).

        Returns:
            None. Prints status messages and performs side effects (junction/registry/files).

        """
        print(f"\n{'='*60}")
        print("gurlatsev/pkg: Package Manager")
        print(f"Scope: {self.scope.value}")
        print(f"{'='*60}\n")

        try:
            try:
                package_path, installing_from_current = self._resolve_install_path(package_path)
            except ValueError as e:
                print(f"ERROR: {e}")
                return

            try:
                metadata = PackageMetadata(package_path)
                metadata.set_scope(self.scope)
                config_data = metadata.load_config(use_defaults=self.use_defaults)
            except Exception as e:
                print(f"ERROR: Failed to load package metadata/config: {e}")
                return

            print(f"Package: {metadata.name}")
            print(f"Version: {metadata.version_string}")
            print(f"Path: {metadata.version_path}")
            print(f"only_portable: {metadata.only_portable}\n")

            if metadata.only_portable and self.scope == Scope.MACHINE:
                print("ERROR: only_portable packages cannot be installed system-wide.")
                print("Please use User scope for only_portable packages.")
                return

            should_install = False
            if installing_from_current:
                print("Installing from resolved 'current' target (skipping junction management)")
                should_install = True
            else:
                print("Managing 'current' junction...")
                try:
                    junction_updated = JunctionManager.update_current_junction_if_needed(metadata, force=self.force)
                except Exception as e:
                    print(f"ERROR: {e}")
                    return
                should_install = junction_updated or metadata.is_current

            if not should_install:
                if self.force:
                    print("\nERROR: --force was requested, but pkg could not update the 'current' junction.")
                    print("Aborting to avoid installing against an unexpected active version.")
                    return

                print("\nSkipping component installation (newer version already installed)")
                print(f"\n{'-'*60}")
                print("Installation complete!")
                print(f"{'-'*60}")
                return

            inconsistencies = metadata.check_metadata_consistency(config_data)
            if inconsistencies:
                if not self.fix_config:
                    print("ERROR: Configuration inconsistencies detected:")
                    for msg in inconsistencies:
                        print(f"  - {msg}")
                    print("\nAborting installation to avoid mutating configs as a side effect.")
                    print("To fix the config, run one of:")
                    print(f"  - pkg --action {Action.UPDATE_CONFIG.value} {metadata.version_path}")
                    print("  - re-run this install with --fix-config")
                    return

                print("WARNING: Configuration inconsistencies detected:")
                for msg in inconsistencies:
                    print(f"  - {msg}")
                print("\n--fix-config enabled: updating configuration file to match directory structure...")
                metadata.update_config(config_data)
                print("Configuration updated successfully.\n")

            print("\nInstalling components...")
            self._install_components(metadata)

            print(f"\n{'-'*60}")
            print("Installation complete!")
            print(f"{'-'*60}")
        finally:
            self._pause()

    def _resolve_install_path(self, package_path: Path) -> Tuple[Path, bool]:
        """Resolve install input into a version directory path.

        Args:
            package_path: User-provided install path.

        Returns:
            A tuple of ``(resolved_path, installing_from_current)``.

        Raises:
            ValueError: If ``current`` is missing/invalid when required.

        """
        current_path = package_path

        # Explicit version directories should be installed directly and must not
        # require sibling "current" junction discovery.
        is_version_dir = is_version_directory_name(package_path.name)
        if is_version_dir:
            return package_path, False

        # If a package root is passed, it must contain a usable "current" junction.
        if package_path.name.lower() != "current":
            current_path = package_path / "current"
            if not current_path.exists():
                raise ValueError(
                    f'No "current" directory exists in package root: {package_path}\n'
                    f"Debug: looked for {current_path}; root_exists={package_path.exists()}, "
                    f"root_is_dir={package_path.is_dir()}"
                )

        # If the path is current, it must be a valid junction with a valid target directory.
        if current_path.name.lower() == "current":
            if not JunctionManager.is_junction(current_path):
                raise ValueError(
                    f'"current" path exists but is not a valid junction: {current_path}\n'
                    f"Debug: exists={current_path.exists()}, is_dir={current_path.is_dir()}, "
                    f"parent={current_path.parent}"
                )

            target = JunctionManager.get_junction_target(current_path)
            if target is None:
                raise ValueError(
                    f'Could not resolve "current" junction target: {current_path}\n'
                    "Debug: os.readlink failed or returned no target."
                )

            resolved_target = target.resolve()
            if not resolved_target.is_dir():
                raise ValueError(
                    f'"current" junction target is not a directory: {resolved_target}\n'
                    f"Debug: source={current_path}, raw_target={target}"
                )

            return resolved_target, True

        # Otherwise install from explicit version directory.
        return package_path, False

    def _install_components(self, metadata: PackageMetadata) -> None:
        """Install all components declared in config for the given package.

        Args:
            metadata: Package metadata loaded from filesystem + config.

        Returns:
            None.

        """
        if metadata.shortcut:
            print("\nCreating shortcuts...")
            ShortcutInstaller.install_shortcuts(metadata)

        if metadata.environment:
            print("\nSetting environment variables...")
            EnvironmentVariableManager.install_environment_variables(metadata)

        print("\nManaging PATH...")
        PATHManager.ensure_bin_in_path(metadata)

        if metadata.path:
            PATHManager.add_to_path(metadata.path, metadata)

        if metadata.bin:
            print("\nCreating executable wrappers...")
            BinFileCreator.install_wrappers(metadata)

    def update_config(self, package_path: Path) -> None:
        """Update (rewrite) the config file from current metadata.

        This normalizes the config and ensures directory-derived fields match the
        filesystem.

        Args:
            package_path: Version directory, ``current`` junction, or package root.

        Returns:
            None.

        """
        try:
            resolved_path, _ = self._resolve_install_path(package_path)
            metadata = PackageMetadata(resolved_path)
            metadata.load_config(use_defaults=self.use_defaults)
            metadata.update_config()
            print(f"Updated configuration for {metadata.name} {metadata.version_string}")
        except Exception as e:
            print(f"ERROR: Failed to update configuration: {e}")

        self._pause()

    def _pause(self) -> None:
        """Pause for a keypress if ``self.pause`` is True."""
        if self.pause:
            print("\nPress any key to continue...")
            try:
                import msvcrt

                msvcrt.getch()
            except ImportError:
                input("Press Enter to continue...")


# =============================================================================
# CLI
# =============================================================================

class _ExtendedHelpAction(argparse.Action):
    """Argparse action that prints standard help plus :data:`EXTENDED_HELP`."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        print("\n" + EXTENDED_HELP.strip() + "\n")
        parser.exit()


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
            "  %(prog)s --action UpdateConfig     # Only update configuration file\n"
            "  %(prog)s --fix-config             # Install and rewrite config if metadata mismatches\n"
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
        help=(
            "Python interpreter override for the pkg.cmd bootstrap launcher. "
            "(Ignored by pkg.py itself; interpreter selection happens before Python starts.)"
        ),
    )

    parser.add_argument(
        "--scope",
        choices=[s.value for s in Scope],
        default=Scope.USER.value,
        help="Installation scope: User (per-user) or Machine (system-wide)",
    )

    parser.add_argument(
        "--action",
        choices=[a.value for a in Action],
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
        help="If config metadata mismatches directory, rewrite pkg.toml instead of aborting",
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

    scope = Scope(args.scope)
    action = Action(args.action)

    manager = PackageManager(
        scope=scope,
        pause=args.pause,
        fix_config=args.fix_config,
        use_defaults=args.use_defaults,
        force=args.force,
        no_autoupdate_config=args.no_autoupdate_config,
    )
    package_path = Path(args.path).resolve()

    if action == Action.INSTALL:
        manager.install(package_path)
    elif action == Action.UPDATE_CONFIG:
        manager.update_config(package_path)
    elif action == Action.COMPRESS:
        raise NotImplementedError("Compress action not yet implemented")
    else:
        print(f"Unknown action: {action}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
