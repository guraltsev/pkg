"""Provide shared models, logging, version handling, expansion, and atomic I/O.

The module contains dependency-light values and utilities used across the pkg
runtime. Filesystem writes use same-directory temporary files so replacements
remain atomic on the destination volume.

Implementation Approach
-----------------------
Small immutable or result-oriented models carry package identity and outcomes.
Pure parsing and expansion helpers are kept beside the narrowly scoped logging
and atomic-write infrastructure shared by higher-level domains.
"""

from __future__ import annotations


import logging
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

__version__ = "0.12.0"
__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4

VERSION_DIR_NAME_RE = re.compile(r"^v(.+)\.l(\d+)$")


class ConfigValidationError(ValueError):
    """Raised when ``pkg.toml`` content is structurally invalid.

    The exception is used for problems the caller can usually fix by editing the
    package configuration, such as unsupported keys, invalid block shapes, or
    missing required fields.
    """


class Scope(Enum):
    """Installation scope supported by ``pkg``.

    ``USER`` targets per-user configuration locations. ``MACHINE`` targets
    machine-wide configuration locations and therefore usually requires
    Administrator privileges.
    """

    USER = "User"
    MACHINE = "Machine"


class Action(Enum):
    """CLI actions supported by the tool.

    ``INSTALL`` applies the package to the selected scope. ``UPDATE_CONFIG``
    only synchronizes configuration metadata back to ``pkg.toml``.
    ``HEALTH_CHECK`` validates package metadata without mutating state.
    """

    INSTALL = "Install"
    UPDATE_CONFIG = "UpdateConfig"
    HEALTH_CHECK = "HealthCheck"
    CHECK_UPDATE = "CheckUpdate"
    UPDATE = "Update"
    AUTO_UPDATE = "AutoUpdate"
    SELF_UPDATE = "SelfUpdate"


@dataclass
class StepResult:
    """Outcome of a single mutation step.

    Attributes
    ----------
    ok : bool
        ``True`` when the step succeeded.
    changed : bool
        ``True`` when the step made a filesystem or registry change.
    warnings : list[str]
        Non-fatal issues emitted while performing the step.
    errors : list[str]
        Fatal issues encountered by the step.

    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class ActionResult:
    """Outcome of a high-level CLI action.

    Attributes
    ----------
    ok : bool
        ``True`` when the action completed successfully.
    changed : bool
        ``True`` when the action made one or more changes.
    warnings : list[str]
        Non-fatal warnings accumulated during the action.
    errors : list[str]
        Fatal errors accumulated during the action.
    exit_code : int
        Process exit code that should be returned to the caller.

    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    exit_code: int = EXIT_SUCCESS


@dataclass(frozen=True)
class PackageIdentity:
    """Directory-derived identity for a package version.

    Attributes
    ----------
    name : str
        Package name derived from the package-root directory name.
    version : str
        Upstream version string derived from the version directory.
    local_version : int
        Local revision number derived from the ``.lN`` suffix.
    version_string : str
        Original version-directory name, for example ``v1.2.3.l4``.
    package_root : Path
        Parent directory that contains all versions and ``current``.
    version_path : Path
        Concrete version directory represented by the identity.
    is_current : bool
        ``True`` when the version already matches ``current``.
    only_portable_by_name : bool
        ``True`` when the package naming convention marks it as portable.

    """

    name: str
    version: str
    local_version: int
    version_string: str
    package_root: Path
    version_path: Path
    is_current: bool
    only_portable_by_name: bool

    @classmethod
    def from_version_path(
        cls,
        package_root: Path,
        version_path: Path,
        *,
        is_current: bool,
    ) -> "PackageIdentity":
        """Construct an identity object from one package root and version path.

        Parameters
        ----------
        package_root : Path
            Directory that owns ``current`` and all version directories.
        version_path : Path
            Concrete version directory represented by the identity.
        is_current : bool
            Whether ``current`` already resolves to ``version_path``.

        Returns
        -------
        PackageIdentity
            Identity derived directly from the filesystem layout.

        Raises
        ------
        ValueError
            Raised when ``version_path`` does not follow the
            ``v<upstream>.l<local>`` naming convention.

        """
        match = VERSION_DIR_NAME_RE.match(version_path.name)
        if not match:
            raise ValueError(
                f"Invalid version directory name: {version_path.name}. Expected format: v<upstream>.l<local>"
            )
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


@dataclass
class ExpansionResult:
    """Result of variable expansion.

    Attributes
    ----------
    value : str
        Expanded text.
    unresolved : list[str]
        Ordered list of unresolved variable tokens left in the output, for
        example ``${MISSING}``.

    """

    value: str
    unresolved: List[str] = field(default_factory=list)


class ExpansionMode(Enum):
    """Ruleset to use when expanding ``$`` variables.

    ``GENERAL`` expands package variables plus braced environment references and
    reports unsupported plain ``$NAME`` tokens as unresolved. ``SCRIPT``
    expands package variables plus braced environment references, but preserves
    plain ``$NAME`` tokens so script languages such as PowerShell keep their
    native variable syntax.
    """

    GENERAL = "general"
    SCRIPT = "script"


class _DynamicStdoutHandler(logging.Handler):
    """Logging handler that writes to the current stdout stream.

    ``contextlib.redirect_stdout()`` swaps out ``sys.stdout`` at runtime. The
    handler writes through :func:`print` during ``emit()`` so test capture keeps
    working even though the logger is configured once at import time.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Render and print one log record to stdout."""
        try:
            print(self.format(record))
        except Exception:
            self.handleError(record)


_LOGGER = logging.getLogger("pkg.stdout")
for _existing_handler in list(_LOGGER.handlers):
    _LOGGER.removeHandler(_existing_handler)
    try:
        _existing_handler.close()
    except Exception:
        pass
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_stdout_handler = _DynamicStdoutHandler()
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
_LOGGER.addHandler(_stdout_handler)


def log_info(message: str) -> None:
    """Emit one informational line to stdout."""
    _LOGGER.info(message)


def log_warning(message: str) -> None:
    """Emit one warning line to stdout with a stable prefix."""
    if not message.startswith("WARNING:"):
        message = f"WARNING: {message}"
    _LOGGER.warning(message)


def log_error(message: str) -> None:
    """Emit one error line to stdout with a stable prefix."""
    if not message.startswith("ERROR:"):
        message = f"ERROR: {message}"
    _LOGGER.error(message)


def is_version_directory_name(name: str) -> bool:
    """Return whether a directory name matches the package version pattern.

    Parameters
    ----------
    name : str
        Directory name to validate.

    Returns
    -------
    bool
        ``True`` when *name* matches ``v<upstream>.l<local>``.

    """
    return VERSION_DIR_NAME_RE.match(name) is not None


def split_package_version(version: str) -> Tuple[str, int]:
    """Split a package version string into upstream and local components.

    Parameters
    ----------
    version : str
        Version text such as ``v1.2.3.l4`` or ``1.2.3``.

    Returns
    -------
    Tuple[str, int]
        A tuple ``(upstream_version, local_revision)``. Missing or malformed
        local revisions are treated as ``0``.

    """
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
    """Compare two package version directory strings.

    The comparison understands upstream semantic-ish version segments,
    prerelease suffixes, and the package-local ``.lN`` revision component.

    Parameters
    ----------
    version1 : str
        Left-hand version string.
    version2 : str
        Right-hand version string.

    Returns
    -------
    int
        ``1`` if *version1* is newer, ``-1`` if *version2* is newer, or ``0``
        if they represent the same version.

    """

    def parse_identifier(token: str) -> Union[int, str]:
        """Parse one dotted version token into a comparable value.

        Parameters
        ----------
        token : str
            One version token, for example ``42`` or ``beta``.

        Returns
        -------
        Union[int, str]
            An ``int`` when the token is numeric; otherwise a lower-cased
            string.

        """
        token = token.strip()
        if token.isdigit():
            return int(token)
        return token.lower()

    def parse_upstream(
        upstream: str,
    ) -> Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]:
        """Parse the upstream portion of a version string.

        Parameters
        ----------
        upstream : str
            Upstream version without the ``.lN`` local revision.

        Returns
        -------
        Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]
            A tuple ``(main_identifiers, prerelease_identifiers)`` where the
            prerelease part is ``None`` for stable releases.

        """
        upstream = upstream.strip()
        if "+" in upstream:
            upstream = upstream.split("+", 1)[0]
        if "-" in upstream:
            main_part, prerelease_part = upstream.split("-", 1)
            prerelease = [
                parse_identifier(part)
                for part in prerelease_part.split(".")
                if part != ""
            ]
        else:
            main_part = upstream
            prerelease = None
        main = [parse_identifier(part) for part in main_part.split(".") if part != ""]
        return main, prerelease

    def compare_identifier_lists(
        left: List[Union[int, str]],
        right: List[Union[int, str]],
        *,
        pad_numeric: bool,
    ) -> int:
        """Compare parsed identifier lists.

        Parameters
        ----------
        left : List[Union[int, str]]
            Parsed identifiers for the left-hand version.
        right : List[Union[int, str]]
            Parsed identifiers for the right-hand version.
        pad_numeric : bool
            Whether to treat missing trailing numeric identifiers as
                zero when one side runs out of tokens.

        Returns
        -------
        int
            ``1`` if *left* is newer, ``-1`` if *right* is newer, or ``0`` if
            they compare equal.

        """
        max_len = max(len(left), len(right))
        for index in range(max_len):
            left_value = (
                left[index] if index < len(left) else (0 if pad_numeric else None)
            )
            right_value = (
                right[index] if index < len(right) else (0 if pad_numeric else None)
            )
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
        prerelease_comparison = compare_identifier_lists(
            prerelease1, prerelease2, pad_numeric=False
        )
        if prerelease_comparison != 0:
            return prerelease_comparison
    if local1 == local2:
        return 0
    return 1 if local1 > local2 else -1


def normalize_path(path: Union[str, Path]) -> Path:
    r"""Normalize a filesystem path for reliable comparisons.

    Parameters
    ----------
    path : Union[str, Path]
        Input path as a string or :class:`~pathlib.Path` instance.

    Returns
    -------
    Path
        A resolved path with any Windows extended-length prefix (``\\?\``)
        removed.

    """
    path_str = str(path)
    if path_str.startswith("\\\\?\\"):
        path_str = path_str[4:]
    return Path(path_str).resolve()


def read_toml_file(path: Path) -> Dict[str, Any]:
    """Read a TOML file into plain Python data.

    Parameters
    ----------
    path : Path
        TOML file to load.

    Returns
    -------
    Dict[str, Any]
        A dictionary-like tree of plain Python values.

    Raises
    ------
    OSError
        If the file cannot be read.
    tomllib.TOMLDecodeError
        If the file is not valid TOML.
    ConfigValidationError
        If the parsed document is not a top-level table.

    """
    with open(path, "rb") as file_handle:
        data = tomllib.load(file_handle)
    if not isinstance(data, dict):
        raise ConfigValidationError(
            f"Configuration must be a TOML table, got: {type(data).__name__}"
        )
    return data


def write_text_atomic(path: Path, text: str, *, backup: bool = False) -> None:
    """Write text atomically to a file.

    Parameters
    ----------
    path : Path
        Destination file path.
    text : str
        Text content to write.
    backup : bool
        Whether to create ``<path>.bak`` before replacing an existing
            file.

    Raises
    ------
    OSError
        If the temporary file or final replacement cannot be written.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as file_handle:
            tmp_fd = None
            file_handle.write(text)
            file_handle.flush()
            os.fsync(file_handle.fileno())
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
    """Write bytes atomically to a file.

    Parameters
    ----------
    path : Path
        Destination file path.
    content : bytes
        Raw bytes to write.

    Raises
    ------
    OSError
        If the temporary file or final replacement cannot be written.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(tmp_fd, "wb") as file_handle:
            tmp_fd = None
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def expand_text(
    text: str, identity: PackageIdentity, mode: ExpansionMode
) -> ExpansionResult:
    """Expand package and environment variables in text.

    Expansion rules:

    - ``$App``, ``$Icons``, and ``$Shortcuts`` expand in every mode.
    - ``${VAR}`` expands in every mode and is tracked as unresolved when the
      environment variable does not exist.
    - Plain ``$NAME`` only expands when it names a package variable.
    - Plain non-package ``$NAME`` tokens are reported as unresolved in
      :class:`ExpansionMode.GENERAL` and stay literal in
      :class:`ExpansionMode.SCRIPT`.
    - ``$$`` becomes a literal ``$``.

    Parameters
    ----------
    text : str
        Source text that may contain variables.
    identity : PackageIdentity
        Package identity used to resolve package-variable paths.
    mode : ExpansionMode
        Expansion ruleset to apply.

    Returns
    -------
    ExpansionResult
        An :class:`ExpansionResult` containing the expanded text and any
        unresolved variable tokens.

    """
    if text is None:
        return ExpansionResult("")

    source = str(text)
    if source == "":
        return ExpansionResult("")

    # Package variables intentionally resolve through ``<package>/current`` so
    # repair installs keep targeting the active package view.
    pkg_base = identity.package_root / "current"
    pkg_map = {
        "App": str(pkg_base / "App"),
        "Icons": str(pkg_base / "Icons"),
        "Shortcuts": str(pkg_base / "Shortcuts"),
    }
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
            else:
                out.append(token)
                if mode == ExpansionMode.GENERAL:
                    unresolved.append(token)
            i = j
            continue

        out.append("$")
        i += 1

    deduplicated_unresolved: List[str] = []
    seen_unresolved = set()
    for token in unresolved:
        if token in seen_unresolved:
            continue
        seen_unresolved.add(token)
        deduplicated_unresolved.append(token)

    return ExpansionResult("".join(out), deduplicated_unresolved)
