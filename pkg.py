#!/usr/bin/env python3
r"""gurlatsev/pkg — Local Package Manager for Windows

Architecture and organization
----------------------------

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
    the per-version config file (``pkg.toml`` preferred, otherwise
    ``pkg.json``). It also computes paths used throughout installation.

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
- ``ConvertJSONToTOML``: convert an existing JSON config to TOML.

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
        pkg.toml          (preferred) or pkg.json

Version directories must be named ``v<upstream>.l<local>``, for example:
``v1.2.3.l1`` or ``v1.2-beta.3.l4``.

Configuration schema (pkg.toml/json)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Top-level keys used by this tool:

- ``name`` (str): must match the directory name.
- ``version`` (str): upstream part (without leading ``v`` or local revision).
- ``localVersion`` (int|str): local revision (the ``lN`` part).
- ``only_portable`` (bool): if true, disallow Machine installs.
- ``environment`` (list[dict]): items with keys ``Name`` and ``Value``.
- ``path`` (list[str]): extra directories to append to PATH.
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

import argparse
import importlib
import json
import os
import re
import site
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import winreg


def get_deps_path() -> Path:
    """Return the local dependency cache directory used by pkg.

    Dependencies are installed under:
    ``%USERPROFILE%\AppData\Local\pkg\_deps``.
    """
    userprofile = os.environ.get("USERPROFILE", str(Path.home()))
    return Path(userprofile) / "AppData" / "Local" / "pkg" / "_deps"


def _ensure_deps_on_path() -> Path:
    """Ensure the local dependency cache is importable via ``sys.path``."""
    deps_path = get_deps_path()
    deps_path.mkdir(parents=True, exist_ok=True)
    site.addsitedir(str(deps_path))
    return deps_path


def ensure_dependency(module_name: str, pip_package: str) -> bool:
    """Import a dependency, auto-installing it into local deps if needed."""
    _ensure_deps_on_path()

    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        pass

    deps_path = get_deps_path()
    print(f"Dependency '{pip_package}' missing; installing to: {deps_path}")
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "--target",
                str(deps_path),
                pip_package,
            ]
        )
    except Exception as exc:
        print(f"Warning: failed to auto-install '{pip_package}': {exc}")
        return False

    # Refresh import path and retry import.
    _ensure_deps_on_path()
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False

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
     workingDirectory = "$App"
     iconLocation = "$Icons\\myapp.ico,0"
     description = "Launch My App"

2) Environment variables (``environment`` list)
   Keys are case-insensitive (canonical: ``Name`` and ``Value``):

     [[environment]]
     Name = "MYAPP_HOME"
     Value = "$App"

3) PATH additions (``path`` list)
   Each entry is appended (if not already present). Entries may include $-vars:

     path = ["$App", "$App\\bin"]

4) Bin wrappers (``bin`` list)
   Each entry is a dict with keys:

   - name (required): file name under the bin dir (e.g. "myapp.cmd")
   - content (required): full file content after variable expansion

   Example:

     [[bin]]
     name = "myapp.cmd"
     content = "@echo off\r\n\"%~dp0\\..\\..\\...\"

Variable expansion rules
~~~~~~~~~~~~~~~~~~~~~~~~

- Package variables are expanded first:
    $App, $Icons, $Shortcuts
  These resolve through the ``current`` junction, so the junction must exist
  and point to the version being installed.

- Environment variables are expanded next:
    $VAR and ${VAR}

Notes
~~~~~

- After registry PATH changes, existing terminals won't automatically see the
  new PATH. Open a new terminal or log off/on.
- If ``toml`` is not installed, JSON is used. To enable TOML support:
    pip install toml
"""


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


# Auto-install optional dependencies into local deps cache.
PYWIN32_AVAILABLE = ensure_dependency("win32com.client", "pywin32")
if PYWIN32_AVAILABLE:
    import win32com.client  # type: ignore

TOML_AVAILABLE = ensure_dependency("toml", "toml")
if TOML_AVAILABLE:
    import toml  # type: ignore


class ConfigValidationError(ValueError):
    """Raised when pkg.toml/pkg.json configuration is invalid."""


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
    CONVERT_JSON_TO_TOML = "ConvertJSONToTOML"
    COMPRESS = "Compress"


class PackageMetadata:
    """Package metadata derived from the filesystem and configuration.

    Instances are created from a *version directory* (``v<upstream>.l<local>``)
    or from a ``current`` junction that points to such a directory.

    Public attributes are populated from:
      1) directory structure, and
      2) configuration file (TOML preferred, else JSON).

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
        self.component_paths: Dict[str, Path] = {}
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
          - ``component_paths`` (App/Icons/Shortcuts subdirectories)
          - ``only_portable`` (inferred from package name suffix ``-portable``)

        Raises:
            ValueError: If the version directory does not match the naming scheme.
        """
        version_dir_name = str(self.version_path.relative_to(self.pkg_path))

        match = re.match(r'^v(.+)\.l(\d+)$', version_dir_name)
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

        self.component_paths = {
            "App": self.version_path / "App",
            "Icons": self.version_path / "Icons",
            "Shortcuts": self.version_path / "Shortcuts",
        }

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
            config_data: Parsed configuration dict (from TOML/JSON).

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
    def _canonicalize_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        """Canonicalize known config keys (case-insensitive) and normalize shapes.

        This makes JSON/TOML keys case-insensitive for the supported schema. In
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

        top_map = {
            # canonical schema keys (case-insensitive) + a few common aliases
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
        }

        out = PackageMetadata._canonicalize_dict_keys(data, top_map, context="config")

        # Normalize absent/null list fields.
        for k in ("environment", "bin", "path", "shortcut"):
            if out.get(k, None) is None:
                out[k] = []

        # Normalize PATH: accept string -> [string] for convenience.
        if isinstance(out.get("path", []), str):
            out["path"] = [out["path"]]

        if not isinstance(out.get("path", []), list):
            raise ConfigValidationError(f"'path' must be a list of strings, got: {type(out['path']).__name__}")

        # Canonicalize list-of-dict blocks.
        env_map = {"name": "Name", "value": "Value"}
        bin_map = {"name": "name", "content": "content"}
        shortcut_map = {
            "name": "name",
            "targetpath": "targetPath",
            "target_path": "targetPath",
            "path": "targetPath",  # user-friendly alias for targetPath
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

        out["environment"] = canonicalize_block("environment", env_map)
        out["bin"] = canonicalize_block("bin", bin_map)
        out["shortcut"] = canonicalize_block("shortcut", shortcut_map)

        # Ensure path entries are strings.
        normalized_path: List[str] = []
        for i, entry in enumerate(out.get("path", [])):
            if entry is None:
                continue
            if not isinstance(entry, str):
                raise ConfigValidationError(f"'path[{i}]' must be a string, got: {type(entry).__name__}")
            normalized_path.append(entry)
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

    def _write_best_guess_toml(self, data: Dict[str, Any]) -> None:
        """Write a best-guess ``pkg.toml`` with commented examples.

        This is used when ``pkg.toml`` is missing. Existing JSON/default metadata
        is used as the source, and commented example sections are included for
        ``shortcut`` and ``bin`` entries.
        """
        toml_path = self.version_path / "pkg.toml"
        data = self._canonicalize_config_dict(data)
        self._validate_config_dict(data)

        lines: List[str] = [
            "# Auto-generated by pkg (best guess).",
            "# You can edit this file manually.",
            f"name = {self._to_toml_scalar(data.get('name', self.name))}",
            f"version = {self._to_toml_scalar(data.get('version', self.version))}",
            f"localVersion = {self._to_toml_scalar(data.get('localVersion', self.local_version))}",
            f"only_portable = {self._to_toml_scalar(data.get('only_portable', self.only_portable))}",
            f"description = {self._to_toml_scalar(data.get('description'))}",
            f"homepage = {self._to_toml_scalar(data.get('homepage'))}",
            f"downloadURL = {self._to_toml_scalar(data.get('downloadURL'))}",
            f"path = {self._to_toml_scalar(data.get('path', []))}",
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
            '# [[bin]]',
            '# name = "myapp.cmd"',
            "# content = \"@echo off && rem call app here\"",
            "",
        ]

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
        print(f"Generated: {toml_path} (best guess)")

    def load_config(self) -> Dict:
        """Load configuration from TOML or JSON.

        TOML (``pkg.toml``) is preferred when the third-party ``toml`` package
        is available; otherwise JSON (``pkg.json``) is used. If neither file
        exists, a default configuration is returned and loaded into the instance.

        Returns:
            The parsed configuration dictionary (or defaults if no file exists).

        Raises:
            RuntimeError: If a JSON file exists but cannot be parsed.
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
        json_path = self.version_path / "pkg.json"

        # Try TOML first
        if toml_path.exists():
            if not TOML_AVAILABLE:
                print("Warning: TOML file found but 'toml' package is not installed.")
                print("Install it with: pip install toml")
                print("Falling back to JSON configuration if present.")
            else:
                try:
                    with open(toml_path, "r", encoding="utf-8") as f:
                        data = toml.load(f)  # type: ignore[name-defined]
                    data = self._canonicalize_config_dict(data)
                    self._validate_config_dict(data)
                    self._load_from_dict(data)
                    return data
                except Exception as e:
                    print(f"Warning: Error loading TOML config: {e}")
                    print("Falling back to JSON configuration if present.")

        # Fall back to JSON
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = self._canonicalize_config_dict(data)
                self._validate_config_dict(data)
                self._load_from_dict(data)
                if not toml_path.exists():
                    self._write_best_guess_toml(data)
                return data
            except Exception as e:
                raise RuntimeError(f"Error loading JSON config from {json_path}: {e}") from e

        # No config file: use defaults and generate a starter TOML.
        default_data = self._canonicalize_config_dict(default_data)
        self._validate_config_dict(default_data)
        self._load_from_dict(default_data)
        if not toml_path.exists():
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
        :class:`PackageMetadata` fields. If ``toml`` is available, TOML is used
        and any existing JSON config is removed. Otherwise JSON is written.

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

        if TOML_AVAILABLE:
            toml_path = self.version_path / "pkg.toml"
            json_path = self.version_path / "pkg.json"

            with open(toml_path, "w", encoding="utf-8") as f:
                toml.dump(data, f)  # type: ignore[name-defined]
            print(f"Updated: {toml_path}")

            if json_path.exists():
                json_path.unlink()
                print(f"Removed: {json_path} (converted to TOML)")
        else:
            json_path = self.version_path / "pkg.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Updated: {json_path}")

    def convert_json_to_toml(self) -> bool:
        """Convert an existing JSON configuration file to TOML.

        Returns:
            True if conversion succeeded (TOML written and JSON removed),
            False otherwise.

        """
        if not TOML_AVAILABLE:
            print("Error: 'toml' package is required for TOML conversion.")
            print("Install it with: pip install toml")
            return False

        json_path = self.version_path / "pkg.json"
        toml_path = self.version_path / "pkg.toml"

        if not json_path.exists():
            print(f"Error: JSON configuration not found at {json_path}")
            return False

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            data = self._canonicalize_config_dict(data)
            self._validate_config_dict(data)
            # Populate metadata from this config so directory-derived portability can be preserved.
            self._load_from_dict(data)

            # Preserve portability flags if not present in JSON.
            if self.only_portable and "only_portable" not in data and "portable" not in data:
                data["only_portable"] = True

            with open(toml_path, "w", encoding="utf-8") as f:
                toml.dump(data, f)  # type: ignore[name-defined]

            json_path.unlink()
            print(f"Converted: {json_path} -> {toml_path}")
            return True

        except Exception as e:
            print(f"Error converting JSON to TOML: {e}")
            return False

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


class VariableExpander:
    """Expand $-style variables in config strings."""

    @staticmethod
    def expand_variables(text: str, metadata: PackageMetadata) -> str:
        """Expand variables in a string using ``$`` syntax.

        Expansion order:

        1) Package variables: ``$App``, ``$Icons``, ``$Shortcuts``.
           These are resolved relative to the package's ``current`` junction, and
           the method verifies that ``current`` points to ``metadata.version_path``.
        2) Environment variables: ``${VAR}`` then ``$VAR``.

        Args:
            text: Input string containing variables.
            metadata: Package metadata (used to resolve package variables).

        Returns:
            The expanded string.

        Raises:
            ValueError: If ``current`` junction does not exist, is not a junction,
                or does not point to the version being installed.

        """
        if not text:
            return text

        current_path = metadata.pkg_path / "current"
        base_path = metadata.version_path

        if not (current_path.exists() and JunctionManager.is_junction(current_path)):
            raise ValueError(f'"current" junction does not exist or is not a junction: ({current_path})')

        try:
            target = JunctionManager.get_junction_target(current_path)
            if not (target and target.resolve() == base_path.resolve()):
                raise ValueError(f'"current" junction is not pointing to this version ({target} != {base_path})')
        except (OSError, AttributeError) as e:
            raise ValueError(f'Failed to read "current" junction target: {current_path}\n Error message: {e}')

        custom_vars = {
            "$App": str(current_path / "App"),
            "$Icons": str(current_path / "Icons"),
            "$Shortcuts": str(current_path / "Shortcuts"),
        }

        for var, value in custom_vars.items():
            text = text.replace(var, value)

        def expand_env_var_braces(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")

        text = re.sub(r"\$\{([^}]+)\}", expand_env_var_braces, text)

        # Expand $VAR tokens conservatively based on whitespace splitting.
        words = text.split()
        expanded_words: List[str] = []
        for word in words:
            if word.startswith("$") and len(word) > 1 and word[1].isalnum():
                var_name = word[1:].split("$")[0]
                for i in range(len(var_name), 0, -1):
                    if var_name[:i].isidentifier():
                        env_value = os.environ.get(var_name[:i], "")
                        word = env_value + word[1 + i :]
                        break
            expanded_words.append(word)

        return " ".join(expanded_words)

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
            if source.exists():
                if JunctionManager.is_junction(source):
                    os.unlink(str(source))
                else:
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

            print(f"JUNCTION error: {result.stderr}")
            return False

        except Exception as e:
            print(f"JUNCTION error creating {source}: {e}")
            return False

    @staticmethod
    def is_junction(path: Path) -> bool:
        """Return True if *path* is an NTFS junction (reparse point).

        Args:
            path: Directory path to test.

        Returns:
            True if the path exists and is detected as a junction; False otherwise.

        Notes:
            - On Python 3.12+, ``os.path.isjunction`` is used if available.
            - On older versions, this function uses a combination of Windows file
              attributes and a fallback ``dir /al`` check.

        """
        try:
            if hasattr(os.path, "isjunction"):
                return os.path.isjunction(str(path))  # type: ignore[attr-defined]

            if not os.path.isdir(str(path)):
                return False

            try:
                st = os.stat(str(path))
                # FILE_ATTRIBUTE_REPARSE_POINT = 0x400 in Windows
                FILE_ATTRIBUTE_REPARSE_POINT = 0x400
                if not (getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
                    return False

                try:
                    os.readlink(str(path))
                    return True
                except (OSError, AttributeError):
                    pass
            except (AttributeError, OSError):
                pass

            result = subprocess.run(
                ["cmd", "/c", "dir", "/al", str(path.parent)],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if result.returncode == 0:
                junction_name = path.name
                for line in result.stdout.splitlines():
                    if "<JUNCTION>" in line.upper() and junction_name in line:
                        return True

            return False

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
    def _parse_version_part(part: str) -> Union[int, str]:
        """Parse one version token for comparisons."""
        return int(part) if part.isdigit() else part.lower()

    @staticmethod
    def compare_versions(version1: str, version2: str) -> int:
        """Compare two version strings of the form ``v<upstream>.l<local>``.

        The comparison is *lexicographic by upstream parts*, where numeric tokens
        compare numerically, and then by the integer local revision.

        Args:
            version1: Version string like ``v1.2.3.l4``.
            version2: Another version string.

        Returns:
            1 if version1 > version2
           -1 if version1 < version2
            0 if equal

        """

        def split_version(v: str) -> Tuple[List[Union[int, str]], int]:
            if v.startswith("v"):
                v = v[1:]

            if ".l" in v:
                upstream_part, local_part = v.rsplit(".l", 1)
                local_rev = int(local_part) if local_part.isdigit() else 0
            else:
                upstream_part = v
                local_rev = 0

            upstream_parts: List[Union[int, str]] = [
                JunctionManager._parse_version_part(p)
                for p in upstream_part.split(".")
                if p
            ]
            return upstream_parts, local_rev

        upstream1, local1 = split_version(version1)
        upstream2, local2 = split_version(version2)

        for i in range(max(len(upstream1), len(upstream2))):
            up1: Union[int, str] = upstream1[i] if i < len(upstream1) else 0
            up2: Union[int, str] = upstream2[i] if i < len(upstream2) else 0

            if type(up1) != type(up2):
                up1, up2 = str(up1), str(up2)

            if up1 > up2:
                return 1
            if up1 < up2:
                return -1

        if local1 > local2:
            return 1
        if local1 < local2:
            return -1
        return 0

    @staticmethod
    def update_current_junction_if_needed(metadata: PackageMetadata) -> bool:
        r"""Update ``<pkg_path>\current`` if the supplied version is not older.

        If ``current`` already exists and points to a newer version, no change is
        made.

        Args:
            metadata: Package metadata for the candidate version.

        Returns:
            True if ``current`` was created/updated; False if it was left unchanged.

        Raises:
            ValueError: If ``current`` exists but is not a junction or points
                somewhere unexpected.

        """
        current_path = metadata.pkg_path / "current"

        if current_path.exists():
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
            if not (current_target.parent == metadata.pkg_path and current_target.is_dir()):
                raise ValueError(
                    f"{current_path} is a junction but its target {current_target} "
                    f"is not a directory under {metadata.pkg_path}. Aborting."
                )

            current_version = current_target.name
            print(f"'current' junction version: {current_version}")
            comparison = JunctionManager.compare_versions(metadata.version_string, current_version)

            if comparison >= 0:
                return JunctionManager.create_junction(current_path, metadata.version_path)

            print(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
            return False

        return JunctionManager.create_junction(current_path, metadata.version_path)


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

            shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[name-defined]
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
        if scope == Scope.USER:
            return winreg.HKEY_CURRENT_USER, r"Environment"
        return winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

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
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as key:
                reg_type = winreg.REG_EXPAND_SZ if expand else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)

            print(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
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
    def get_current_path(scope: Scope) -> List[str]:
        """Read the current PATH entries from the registry.

        Args:
            scope: User or Machine scope.

        Returns:
            A list of PATH entries (strings). Missing/empty PATH returns [].

        """
        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                value, reg_type = winreg.QueryValueEx(key, "Path")
                if reg_type in (winreg.REG_EXPAND_SZ, winreg.REG_SZ):
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
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, path_value)
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

        for entry in expanded_new_entries:
            if entry and entry not in updated_path:
                updated_path.append(entry)
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

        if bin_dir_str not in current_path:
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
            with open(wrapper_path, "w", encoding="utf-8") as f:
                f.write(expanded_content)

            print(f"BIN: created: {wrapper_path}")
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


class PackageManager:
    """Orchestrate install/update operations for a package."""

    def __init__(self, scope: Scope = Scope.USER, pause: bool = False, no_autoupdate_config: bool = False):
        """Create a :class:`PackageManager`.

        Args:
            scope: Installation scope.
            pause: If True, pause for a keypress before exiting (useful in double-click scenarios).
            no_autoupdate_config:
                If True, abort installs when config mismatches directory metadata instead of rewriting config.

        Raises:
            SystemExit: If Machine scope is requested without Administrator privileges.

        """
        self.scope = scope
        self.pause = pause
        self.no_autoupdate_config = no_autoupdate_config

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
        print("gu-opt-pkg: Package Manager")
        print(f"Scope: {self.scope.value}")
        print(f"{'='*60}\n")

        # If the user passed the package root, install from its "current" junction.
        if package_path.is_dir() and (package_path / "current").exists() and package_path.name.lower() != "current":
            maybe_current = package_path / "current"
            if JunctionManager.is_junction(maybe_current):
                package_path = maybe_current

        try:
            metadata = PackageMetadata(package_path)
            metadata.set_scope(self.scope)
            config_data = metadata.load_config()
        except Exception as e:
            print(f"ERROR: Failed to parse package metadata: {e}")
            self._pause()
            return

        print(f"Package: {metadata.name}")
        print(f"Version: {metadata.version_string}")
        print(f"Path: {metadata.version_path}")
        print(f"only_portable: {metadata.only_portable}\n")

        inconsistencies = metadata.check_metadata_consistency(config_data)
        if inconsistencies:
            if self.no_autoupdate_config:
                print("ERROR: Configuration inconsistencies detected and --no-autoupdate-config is enabled:")
                for msg in inconsistencies:
                    print(f"  - {msg}")
                print("\nAborting installation. Please fix the configuration manually.")
                self._pause()
                return

            print("WARNING: Configuration inconsistencies detected:")
            for msg in inconsistencies:
                print(f"  - {msg}")
            print("\nAuto-updating configuration file to match directory structure...")
            metadata.update_config(config_data)
            print("Configuration updated successfully.\n")

        if metadata.only_portable and self.scope == Scope.MACHINE:
            print("ERROR: only_portable packages cannot be installed system-wide.")
            print("Please use User scope for only_portable packages.")
            self._pause()
            return

        if package_path.name.lower() == "current" and JunctionManager.is_junction(package_path):
            print("Installing from 'current' junction (skipping junction management)")
            self._install_components(metadata)
        else:
            print("Managing 'current' junction...")
            junction_updated = JunctionManager.update_current_junction_if_needed(metadata)

            if junction_updated or metadata.is_current:
                print("\nInstalling components...")
                self._install_components(metadata)
            else:
                print("\nSkipping component installation (newer version already installed)")

        print(f"\n{'-'*60}")
        print("Installation complete!")
        print(f"{'-'*60}")
        self._pause()

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
        if package_path.is_dir() and (package_path / "current").exists() and package_path.name.lower() != "current":
            maybe_current = package_path / "current"
            if JunctionManager.is_junction(maybe_current):
                package_path = maybe_current

        try:
            metadata = PackageMetadata(package_path)
            metadata.load_config()
            metadata.update_config()
            print(f"Updated configuration for {metadata.name} {metadata.version_string}")
        except Exception as e:
            print(f"ERROR: Failed to update configuration: {e}")

        self._pause()

    def convert_json_to_toml(self, package_path: Path) -> None:
        """Convert a version's JSON config to TOML.

        Args:
            package_path: Version directory, ``current`` junction, or package root.

        Returns:
            None.

        """
        if package_path.is_dir() and (package_path / "current").exists() and package_path.name.lower() != "current":
            maybe_current = package_path / "current"
            if JunctionManager.is_junction(maybe_current):
                package_path = maybe_current

        try:
            metadata = PackageMetadata(package_path)
            if metadata.convert_json_to_toml():
                print("Successfully converted JSON to TOML")
            else:
                print("Failed to convert JSON to TOML")
        except Exception as e:
            print(f"ERROR: Failed to convert JSON to TOML: {e}")

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
        description="Local Package Manager for Windows (gu-opt-pkg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                         # Install in User scope from current directory\n"
            "  %(prog)s --scope Machine          # Install system-wide (requires admin)\n"
            "  %(prog)s --action UpdateConfig     # Only update configuration file\n"
            "  %(prog)s --action ConvertJSONToTOML# Convert JSON config to TOML\n"
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
        help="Abort if configuration mismatches directory metadata (do not rewrite config)",
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

    manager = PackageManager(scope=scope, pause=args.pause, no_autoupdate_config=args.no_autoupdate_config)
    package_path = Path(args.path).resolve()

    if action == Action.INSTALL:
        manager.install(package_path)
    elif action == Action.UPDATE_CONFIG:
        manager.update_config(package_path)
    elif action == Action.CONVERT_JSON_TO_TOML:
        manager.convert_json_to_toml(package_path)
    elif action == Action.COMPRESS:
        raise NotImplementedError("Compress action not yet implemented")
    else:
        print(f"Unknown action: {action}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
