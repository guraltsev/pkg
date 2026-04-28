#!/usr/bin/env python3
"""Single-file implementation of ``pkg``.

The package manager implementation now lives entirely in ``pkg.py`` while still
keeping Windows-specific mutation code strictly isolated from package-management
logic through clearly labeled sections in this file.

Documentation map
-----------------

- ``README.md`` explains the tool from a user perspective.
- ``docs/README.md`` is the documentation index.
- ``docs/architecture.md`` describes the single-file section boundaries.
- ``docs/configuration.md`` documents ``pkg.toml``.
- ``docs/api.md`` summarizes the public API.
- ``docs/review.md`` records the review findings and applied changes.

Section guide
-------------

- ``Shared models and pure helpers`` contains shared data models, validation
  helpers, text expansion, version comparison, and atomic file writers.
- ``Windows integration boundary`` contains only thin Python wrappers around
  direct Windows primitives such as shortcut creation, registry access,
  junction creation, privilege checks, and environment-change broadcasts.
- ``Package-management logic and CLI`` contains config normalization, metadata
  synchronization, runtime validation, and the classes that orchestrate those
  Windows wrappers.
"""

from __future__ import annotations

#------------------------------------------
# Section: Shared models and pure helpers
#------------------------------------------
import json
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
__copyright__ = "Copyright (C) 2025 Gennady Uraltseev. All rights reserved."
__license__ = "MIT"

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4

VERSION_DIR_NAME_RE = re.compile(r"^v(.+)\.l(\d+)$")
PACKAGE_VARIABLE_NAMES = ("App", "Icons", "Shortcuts")


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
    """

    INSTALL = "Install"
    UPDATE_CONFIG = "UpdateConfig"


@dataclass
class StepResult:
    """Outcome of a single mutation step.

    Attributes:
        ok: ``True`` when the step succeeded.
        changed: ``True`` when the step made a filesystem/registry change.
        warnings: Non-fatal issues emitted while performing the step.
        errors: Fatal issues encountered by the step.
    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class ActionResult:
    """Outcome of a high-level CLI action.

    Attributes:
        ok: ``True`` when the action completed successfully.
        changed: ``True`` when the action made one or more changes.
        warnings: Non-fatal warnings accumulated during the action.
        errors: Fatal errors accumulated during the action.
        exit_code: Process exit code that should be returned to the caller.
    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    exit_code: int = EXIT_SUCCESS


@dataclass(frozen=True)
class ResolvedInput:
    """Normalized interpretation of a user-supplied package path.

    Attributes:
        raw_path: Path as supplied by the caller after ``expanduser()``.
        package_root: Directory that owns the ``current`` junction and version
            directories.
        version_path: Concrete version directory that should be operated on.
        input_kind: Classification of the input path, such as ``version``,
            ``current``, or ``package_root``.
        installing_from_current: ``True`` when the user pointed at ``current``
            or the package root and the current junction was already resolved.
        version_is_current: ``True`` when :attr:`version_path` is already the
            active ``current`` target for the package.
    """

    raw_path: Path
    package_root: Path
    version_path: Path
    input_kind: str
    installing_from_current: bool
    version_is_current: bool = False


@dataclass(frozen=True)
class PackageIdentity:
    """Directory-derived identity for a package version.

    Attributes:
        name: Package name derived from the package-root directory name.
        version: Upstream version string derived from the version directory.
        local_version: Local revision number derived from the ``.lN`` suffix.
        version_string: Original version-directory name, e.g. ``v1.2.3.l4``.
        package_root: Parent directory that contains all versions and
            ``current``.
        version_path: Concrete version directory represented by the identity.
        is_current: ``True`` when the version already matches ``current``.
        only_portable_by_name: ``True`` when the package name convention marks
            it as portable (``*-portable``).
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
    def from_resolved_input(cls, resolved: ResolvedInput) -> "PackageIdentity":
        """Construct an identity object from a resolved package path.

        Args:
            resolved: Classified path information produced by the platform path
                resolver.

        Returns:
            A :class:`PackageIdentity` derived from the version directory name
            and package-root location.

        Raises:
            ValueError: If :attr:`ResolvedInput.version_path` does not follow
                the ``v<upstream>.l<local>`` naming convention.
        """

        match = VERSION_DIR_NAME_RE.match(resolved.version_path.name)
        if not match:
            raise ValueError(
                f"Invalid version directory name: {resolved.version_path.name}. Expected format: v<upstream>.l<local>"
            )
        package_root = resolved.package_root
        version_path = resolved.version_path
        return cls(
            name=package_root.name,
            version=match.group(1),
            local_version=int(match.group(2)),
            version_string=version_path.name,
            package_root=package_root,
            version_path=version_path,
            is_current=resolved.version_is_current,
            only_portable_by_name=package_root.name.lower().endswith("-portable"),
        )


@dataclass(frozen=True)
class ScopePaths:
    """Resolved filesystem and registry locations for one installation scope.

    Attributes:
        scope: Installation scope the paths belong to.
        shortcut_root: Directory under which Start Menu shortcuts are created.
        bin_dir: Directory where wrapper files should be written.
        env_root: Registry hive object/constant for environment updates.
        env_subkey: Registry subkey path for environment updates.
    """

    scope: Scope
    shortcut_root: Path
    bin_dir: Path
    env_root: Any
    env_subkey: str


@dataclass
class ShortcutSpec:
    """Runtime model for one shortcut declaration.

    Attributes:
        name: Display name or file name for the shortcut.
        target_path: Executable path the shortcut should launch.
        arguments: Optional command-line arguments.
        working_directory: Optional working directory.
        icon_location: Optional ``path,index`` icon reference.
        description: Optional description shown by Windows.
    """

    name: str
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


@dataclass
class EnvVarSpec:
    """Runtime model for one environment-variable declaration.

    Attributes:
        name: Environment variable name.
        value: Value to write to the registry.
    """

    name: str
    value: str


@dataclass
class BinSpec:
    """Runtime model for one wrapper-file declaration.

    Attributes:
        name: File name that should be created in the scope bin directory.
        content: Wrapper script or batch content to write.
    """

    name: str
    content: str


@dataclass
class PackageConfig:
    """Normalized runtime configuration for a package version.

    Attributes:
        description: Optional package description.
        homepage: Optional homepage URL.
        download_url: Optional download URL.
        only_portable: Package-level portability restriction.
        environment: Environment variables to write.
        shortcut: Shortcuts to create.
        path: Extra PATH entries to add.
        bin: Wrapper files to create.
    """

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
    """Result of variable expansion.

    Attributes:
        value: Expanded text.
        unresolved: Ordered list of unresolved variable tokens left in the
            output, for example ``${MISSING}``.
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


@dataclass(frozen=True)
class PreparedShortcut:
    """Fully expanded shortcut data ready for backend-specific creation.

    Attributes:
        name: Final shortcut name without any unresolved variables.
        shortcut_path: Full ``.lnk`` path to create.
        target_path: Executable path to launch.
        arguments: Final argument string.
        working_directory: Final working directory.
        icon_location: Final icon reference.
        description: Final description string.
    """

    name: str
    shortcut_path: Path
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


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

    Args:
        name: Directory name to validate.

    Returns:
        ``True`` when *name* matches ``v<upstream>.l<local>``.
    """

    return VERSION_DIR_NAME_RE.match(name) is not None


def split_package_version(version: str) -> Tuple[str, int]:
    """Split a package version string into upstream and local components.

    Args:
        version: Version text such as ``v1.2.3.l4`` or ``1.2.3``.

    Returns:
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

    Args:
        version1: Left-hand version string.
        version2: Right-hand version string.

    Returns:
        ``1`` if *version1* is newer, ``-1`` if *version2* is newer, or ``0``
        if they represent the same version.
    """

    def parse_identifier(token: str) -> Union[int, str]:
        """Parse one dotted version token into a comparable value.

        Args:
            token: One version token, for example ``42`` or ``beta``.

        Returns:
            An ``int`` when the token is numeric; otherwise a lower-cased
            string.
        """

        token = token.strip()
        if token.isdigit():
            return int(token)
        return token.lower()

    def parse_upstream(upstream: str) -> Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]:
        """Parse the upstream portion of a version string.

        Args:
            upstream: Upstream version without the ``.lN`` local revision.

        Returns:
            A tuple ``(main_identifiers, prerelease_identifiers)`` where the
            prerelease part is ``None`` for stable releases.
        """

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

    def compare_identifier_lists(
        left: List[Union[int, str]],
        right: List[Union[int, str]],
        *,
        pad_numeric: bool,
    ) -> int:
        """Compare parsed identifier lists.

        Args:
            left: Parsed identifiers for the left-hand version.
            right: Parsed identifiers for the right-hand version.
            pad_numeric: Whether to treat missing trailing numeric identifiers as
                zero when one side runs out of tokens.

        Returns:
            ``1`` if *left* is newer, ``-1`` if *right* is newer, or ``0`` if
            they compare equal.
        """

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

    Args:
        path: Input path as a string or :class:`~pathlib.Path` instance.

    Returns:
        A resolved path with any Windows extended-length prefix (``\\?\``)
        removed.
    """

    path_str = str(path)
    if path_str.startswith('\\\\?\\'):
        path_str = path_str[4:]
    return Path(path_str).resolve()


def read_toml_file(path: Path) -> Dict[str, Any]:
    """Read a TOML file into plain Python data.

    Args:
        path: TOML file to load.

    Returns:
        A dictionary-like tree of plain Python values.

    Raises:
        OSError: If the file cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        ConfigValidationError: If the parsed document is not a top-level table.
    """

    with open(path, "rb") as file_handle:
        data = tomllib.load(file_handle)
    if not isinstance(data, dict):
        raise ConfigValidationError(f"Configuration must be a TOML table, got: {type(data).__name__}")
    return data

def write_text_atomic(path: Path, text: str, *, backup: bool = False) -> None:
    """Write text atomically to a file.

    Args:
        path: Destination file path.
        text: Text content to write.
        backup: Whether to create ``<path>.bak`` before replacing an existing
            file.

    Raises:
        OSError: If the temporary file or final replacement cannot be written.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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

    Args:
        path: Destination file path.
        content: Raw bytes to write.

    Raises:
        OSError: If the temporary file or final replacement cannot be written.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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


def combine_step_results(*results: StepResult) -> StepResult:
    """Merge several step outcomes into a single aggregate outcome.

    Args:
        *results: Step results to combine.

    Returns:
        A :class:`StepResult` whose flags and message lists reflect all supplied
        results.
    """

    combined = StepResult(ok=True, changed=False)
    for result in results:
        combined.ok = combined.ok and result.ok
        combined.changed = combined.changed or result.changed
        combined.warnings.extend(result.warnings)
        combined.errors.extend(result.errors)
    if combined.errors:
        combined.ok = False
    return combined


def _deduplicate_preserving_order(values: List[str]) -> List[str]:
    """Remove duplicates from a list while preserving the first occurrence.

    Args:
        values: Sequence of strings that may contain duplicates.

    Returns:
        A new list containing only the first occurrence of each value.
    """

    seen = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _package_variable_map(identity: PackageIdentity) -> Dict[str, str]:
    """Build the package-variable expansion map for one package identity.

    Package variables intentionally resolve through ``<package>/current``
    rather than directly through :attr:`PackageIdentity.version_path`.
    Re-running install for the same version is a supported repair path, so
    shortcuts, environment variables, PATH entries, and wrapper files keep
    targeting the active package view.

    Args:
        identity: Package identity whose ``current`` tree should be referenced.

    Returns:
        Mapping from package variable name to filesystem path.
    """

    pkg_base = identity.package_root / "current"
    return {
        "App": str(pkg_base / "App"),
        "Icons": str(pkg_base / "Icons"),
        "Shortcuts": str(pkg_base / "Shortcuts"),
    }


def expand_text(text: str, identity: PackageIdentity, mode: ExpansionMode) -> ExpansionResult:
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

    Args:
        text: Source text that may contain variables.
        identity: Package identity used to resolve package-variable paths.
        mode: Expansion ruleset to apply.

    Returns:
        An :class:`ExpansionResult` containing the expanded text and any
        unresolved variable tokens.
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
            else:
                out.append(token)
                if mode == ExpansionMode.GENERAL:
                    unresolved.append(token)
            i = j
            continue

        out.append("$")
        i += 1

    return ExpansionResult("".join(out), _deduplicate_preserving_order(unresolved))


#------------------------------------------
# Section: Windows integration boundary
#------------------------------------------
import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

if os.name == "nt":
    import winreg
else:
    winreg = None  # type: ignore[assignment]


def require_winreg() -> Any:
    """Return the :mod:`winreg` module or raise a platform error.

    Returns:
        The imported :mod:`winreg` module.

    Raises:
        OSError: If the current interpreter does not provide :mod:`winreg`.
    """

    if winreg is None:
        raise OSError("winreg is only available on Windows.")
    return winreg


def _run_hidden(command: List[str]) -> subprocess.CompletedProcess:
    """Run one Windows command without opening a console window.

    Args:
        command: Command-line tokens to execute.

    Returns:
        The completed subprocess result.
    """

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _escape_powershell_single_quoted(value: str) -> str:
    """Escape text for a PowerShell single-quoted string literal.

    Args:
        value: Text that will be embedded in PowerShell.

    Returns:
        The same text with apostrophes doubled.
    """

    return value.replace("'", "''")


def create_shortcut(prepared: PreparedShortcut) -> None:
    """Create one ``.lnk`` file through PowerShell automation.

    Args:
        prepared: Fully expanded shortcut data to write.

    Raises:
        RuntimeError: If PowerShell reports a shortcut-creation failure.
    """

    ps_command = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{_escape_powershell_single_quoted(str(prepared.shortcut_path))}')
$Shortcut.TargetPath = '{_escape_powershell_single_quoted(prepared.target_path)}'
$Shortcut.Arguments = '{_escape_powershell_single_quoted(prepared.arguments)}'
$Shortcut.WorkingDirectory = '{_escape_powershell_single_quoted(prepared.working_directory)}'
$Shortcut.IconLocation = '{_escape_powershell_single_quoted(prepared.icon_location)}'
$Shortcut.Description = '{_escape_powershell_single_quoted(prepared.description)}'
$Shortcut.Save()
"""
    result = _run_hidden([
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        ps_command,
    ])
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "unknown PowerShell shortcut error").strip()
        raise RuntimeError(error_text)


def create_junction(source: Path, target: Path) -> None:
    r"""Create or replace one NTFS junction.

    Args:
        source: Path where the junction should be created.
        target: Existing directory the junction should reference.

    Raises:
        RuntimeError: If the junction cannot be created safely.
    """

    if not target.exists() or not target.is_dir():
        raise RuntimeError(f"Junction target does not exist or is not a directory: {target}")

    if os.path.lexists(str(source)):
        if is_junction(source):
            os.rmdir(str(source))
        else:
            raise RuntimeError(
                f"{source} already exists and is not a junction; refusing to overwrite."
            )

    result = _run_hidden(["cmd", "/c", "mklink", "/J", str(source), str(target)])
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "mklink /J failed").strip()
        raise RuntimeError(error_text)


def _win_get_reparse_tag(path: Path) -> Optional[int]:
    """Read the reparse tag for one filesystem entry.

    Args:
        path: Filesystem entry to inspect.

    Returns:
        The integer reparse tag, or ``None`` when the tag cannot be read.
    """

    try:
        import ctypes
        from ctypes import wintypes

        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        OPEN_EXISTING = 3
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        FSCTL_GET_REPARSE_POINT = 0x000900A8

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        device_io_control = ctypes.windll.kernel32.DeviceIoControl
        device_io_control.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        device_io_control.restype = wintypes.BOOL

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle_value = wintypes.HANDLE(-1).value
        if handle == invalid_handle_value:
            return None

        try:
            buffer = ctypes.create_string_buffer(16 * 1024)
            returned = wintypes.DWORD(0)
            ok = device_io_control(
                handle,
                FSCTL_GET_REPARSE_POINT,
                None,
                0,
                buffer,
                len(buffer),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return int.from_bytes(buffer.raw[0:4], "little", signed=False)
        finally:
            close_handle(handle)
    except Exception:
        return None


def is_junction(path: Path) -> bool:
    """Return whether one path is an NTFS junction.

    Args:
        path: Filesystem entry to inspect.

    Returns:
        ``True`` when *path* exists and is a junction; otherwise ``False``.
    """

    try:
        if hasattr(os.path, "isjunction"):
            return os.path.isjunction(str(path))  # type: ignore[attr-defined]

        if not os.path.isdir(str(path)):
            return False

        io_reparse_tag_mount_point = 0xA0000003
        return _win_get_reparse_tag(path) == io_reparse_tag_mount_point
    except Exception:
        return False


def get_junction_target(path: Path) -> Optional[Path]:
    """Resolve the target of one junction path.

    Args:
        path: Junction path to inspect.

    Returns:
        The normalized target path, or ``None`` when the target cannot be read.
    """

    try:
        return normalize_path(os.readlink(str(path)))
    except (OSError, AttributeError):
        return None


def environment_registry_location(scope: Scope) -> Tuple[Any, str]:
    """Return the registry location used for one environment scope.

    Args:
        scope: Installation scope whose environment location is needed.

    Returns:
        A tuple ``(root_hkey_or_none, subkey)``.
    """

    if scope == Scope.USER:
        return (winreg.HKEY_CURRENT_USER if winreg is not None else None, r"Environment")
    return (
        winreg.HKEY_LOCAL_MACHINE if winreg is not None else None,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )


def read_registry_value(root: Any, subkey: str, name: str) -> Tuple[Any, int]:
    """Read one registry value.

    Args:
        root: Registry hive constant.
        subkey: Registry key path below *root*.
        name: Value name to read.

    Returns:
        Tuple ``(value, registry_type)`` from ``QueryValueEx``.

    Raises:
        OSError: If the value cannot be read.
    """

    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_READ) as key:
        return reg.QueryValueEx(key, name)


def write_registry_value(root: Any, subkey: str, name: str, value: str, reg_type: int) -> None:
    """Write one registry value.

    Args:
        root: Registry hive constant.
        subkey: Registry key path below *root*.
        name: Value name to write.
        value: Value data to store.
        reg_type: ``winreg`` registry type constant.

    Raises:
        OSError: If the value cannot be written.
    """

    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, name, 0, reg_type, value)


def broadcast_environment_change() -> None:
    """Notify Windows that environment values changed.

    Raises:
        OSError: If Windows does not accept the broadcast notification.
    """

    import ctypes
    from ctypes import wintypes

    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002

    send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
    send_message_timeout.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(wintypes.ULONG_PTR),
    ]
    send_message_timeout.restype = wintypes.LPARAM

    result = wintypes.ULONG_PTR(0)
    ok = send_message_timeout(
        hwnd_broadcast,
        wm_settingchange,
        0,
        ctypes.cast(ctypes.c_wchar_p("Environment"), wintypes.LPARAM),
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )
    if not ok:
        raise OSError("SendMessageTimeoutW failed")


def is_current_user_admin() -> bool:
    """Return whether the current process has Administrator privileges.

    Returns:
        ``True`` when the current process is elevated; otherwise ``False``.
    """

    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def wait_for_keypress() -> None:
    """Pause for a keypress using the Windows console when possible.

    Returns:
        ``None``.
    """

    try:
        import msvcrt

        msvcrt.getch()
    except ImportError:
        input("Press Enter to continue...")


def system_drive_root() -> str:
    """Return the system drive root with a trailing backslash.

    Returns:
        A drive-root string such as ``C:\\``.
    """

    system_drive = os.environ.get("SYSTEMDRIVE", "C:")
    if not system_drive.endswith("\\"):
        system_drive += "\\"
    return system_drive


#------------------------------------------
# Section: Package-management logic and CLI
#------------------------------------------
import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

InstallStep = Callable[["PackageMetadata"], StepResult]


def compute_scope_paths(scope: Scope) -> ScopePaths:
    """Resolve the filesystem and registry locations for one install scope.

    Args:
        scope: Installation scope for which paths should be calculated.

    Returns:
        A :class:`ScopePaths` object describing the relevant directories and
        registry location.

    Raises:
        ValueError: If required environment variables such as ``APPDATA`` or
            ``PROGRAMDATA`` are missing.
    """

    env_root, env_subkey = environment_registry_location(scope)
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
            env_root=env_root,
            env_subkey=env_subkey,
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
        env_root=env_root,
        env_subkey=env_subkey,
    )


def _expanded_text_or_error(
    text: str,
    identity: PackageIdentity,
    mode: ExpansionMode,
    *,
    field_label: str,
) -> str:
    """Expand text or raise a clear unresolved-variable error.

    Args:
        text: Source text to expand.
        identity: Package identity that supplies package-variable paths.
        mode: Expansion ruleset to use.
        field_label: Human-readable label used in error messages.

    Returns:
        Fully expanded text.

    Raises:
        ValueError: If the expansion leaves unresolved variable tokens.
    """

    expansion = expand_text(text, identity, mode)
    if expansion.unresolved:
        unresolved = ", ".join(expansion.unresolved)
        raise ValueError(f"{field_label} contains unresolved variable(s): {unresolved}")
    return expansion.value


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _warn_if_output_path_is_unusual(kind: str, default_root: Path, expanded_name: str, final_path: Path) -> None:
    """Warn when a shortcut/bin output lands outside its default root.

    Relative nested paths inside the default root are allowed without warning.
    Absolute names and escaping parent traversal remain allowed, but they are
    noisy enough that install should call them out explicitly.
    """

    looks_absolute = expanded_name.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(expanded_name) is not None

    depth = 0
    escapes_by_parent_traversal = False
    for segment in re.split(r"[\\/]+", expanded_name):
        if segment in ("", "."):
            continue
        if segment == "..":
            if depth == 0:
                escapes_by_parent_traversal = True
                break
            depth -= 1
            continue
        depth += 1

    outside_default_root = escapes_by_parent_traversal
    try:
        outside_default_root = outside_default_root or not final_path.resolve(strict=False).is_relative_to(
            default_root.resolve(strict=False)
        )
    except OSError:
        outside_default_root = True

    if looks_absolute or outside_default_root:
        destination = expanded_name if looks_absolute else str(final_path)
        log_warning(
            f"{kind} output resolves outside the default {kind} root; this is allowed but unusual: {destination}"
        )


def prepare_shortcut_spec(
    spec: ShortcutSpec,
    identity: PackageIdentity,
    scope_paths: ScopePaths,
) -> PreparedShortcut:
    """Expand, validate, and materialize one shortcut definition.

    Args:
        spec: Normalized shortcut declaration.
        identity: Package identity used for variable expansion.
        scope_paths: Scope-specific paths used to determine the Start Menu
            destination.

    Returns:
        A :class:`PreparedShortcut` ready to be passed to a backend-specific
        shortcut writer.

    Raises:
        ValueError: If required fields are empty after expansion or variables
            remain unresolved.
    """

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
    _warn_if_output_path_is_unusual("shortcut", scope_paths.shortcut_root, expanded_name, shortcut_path)
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


def _current_version_matches(package_root: Path, version_path: Path) -> bool:
    """Return whether ``package_root/current`` points at ``version_path``.

    Args:
        package_root: Package root that may contain the ``current`` junction.
        version_path: Concrete version directory to compare against.

    Returns:
        ``True`` when ``current`` exists, is a junction, and resolves to
        *version_path*.
    """

    current_path = package_root / "current"
    if not current_path.exists() or not is_junction(current_path):
        return False
    target = get_junction_target(current_path)
    if target is None:
        return False
    try:
        return normalize_path(target) == normalize_path(version_path)
    except OSError:
        return False


def _normalize_input_path(raw_path: Path) -> Path:
    """Normalize a user-supplied path without dereferencing junctions.

    Args:
        raw_path: User-supplied path that may be relative, including ``.`` or
            ``..`` segments.

    Returns:
        An absolute, lexically normalized path that preserves a trailing
        ``current`` path component instead of resolving through it.
    """

    return Path(os.path.abspath(os.fspath(raw_path.expanduser())))


def resolve_input_path(raw_path: Path) -> ResolvedInput:
    """Classify a user-supplied path using ``pkg`` package-layout rules.

    Args:
        raw_path: User-supplied path that may point at a version directory, a
            ``current`` junction, or the package root.

    Returns:
        A :class:`ResolvedInput` describing the package root, concrete version
        directory, and whether the path was resolved via ``current``.

    Raises:
        ValueError: If the path does not match a supported package layout.
    """

    candidate = _normalize_input_path(raw_path)

    if is_version_directory_name(candidate.name):
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"Version directory does not exist: {candidate}")
        package_root = candidate.parent
        return ResolvedInput(
            raw_path=candidate,
            package_root=package_root,
            version_path=candidate,
            input_kind="version",
            installing_from_current=False,
            version_is_current=_current_version_matches(package_root, candidate),
        )

    if candidate.name.lower() == "current":
        if not candidate.exists():
            raise ValueError(f'"current" path does not exist: {candidate}')
        if not is_junction(candidate):
            raise ValueError(
                f'"current" path exists but is not a valid junction: {candidate}; '
                f"exists={candidate.exists()}, is_dir={candidate.is_dir()}, parent={candidate.parent}"
            )
        target = get_junction_target(candidate)
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
            version_is_current=True,
        )

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Package root does not exist: {candidate}")
    current_path = candidate / "current"
    if not current_path.exists():
        raise ValueError(
            f'No "current" directory exists in package root: {candidate}; '
            f"looked for {current_path}; root_exists={candidate.exists()}, root_is_dir={candidate.is_dir()}"
        )
    if not is_junction(current_path):
        raise ValueError(
            f'"current" path exists but is not a valid junction: {current_path}; '
            f"exists={current_path.exists()}, is_dir={current_path.is_dir()}, parent={current_path.parent}"
        )
    target = get_junction_target(current_path)
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
        version_is_current=True,
    )


class JunctionManager:
    """Decide when and how ``pkg`` should repoint the ``current`` junction."""

    @staticmethod
    def compare_versions(version1: str, version2: str) -> int:
        """Compare two package version directory names.

        Args:
            version1: Left-hand version string.
            version2: Right-hand version string.

        Returns:
            ``1`` when *version1* is newer, ``-1`` when *version2* is newer, or
            ``0`` when they match.
        """

        return compare_package_versions(version1, version2)

    @staticmethod
    def update_current_junction_if_needed(metadata: "PackageMetadata", *, force: bool = False) -> bool:
        r"""Update ``<package>\current`` unless a newer version should win.

        When this method runs for the version that is already active, it may
        still refresh ``current`` by recreating the junction. That behavior is
        intentional: install uses reruns as a repair path for external state,
        so same-version targets are not treated as a junction no-op here.

        Args:
            metadata: Package metadata describing the version being installed.
            force: Whether to allow replacing ``current`` when it already
                points to a newer version. Same-version targets may still
                refresh ``current`` without ``force``.

        Returns:
            ``True`` when ``current`` was recreated or repointed; ``False``
            only when ``current`` was intentionally left untouched because a
            newer version was already active.

        Raises:
            ValueError: If the existing ``current`` path is unsafe or malformed.
            RuntimeError: If the junction replacement fails.
        """

        current_path = metadata.pkg_path / "current"
        desired_target = metadata.version_path

        if not desired_target.exists() or not desired_target.is_dir():
            raise RuntimeError(f"Junction target does not exist or is not a directory: {desired_target}")

        if os.path.lexists(str(current_path)):
            if not is_junction(current_path):
                raise ValueError(f"{current_path} exists but is not a junction. Aborting all operations.")

            current_target = get_junction_target(current_path)
            if current_target is None:
                raise ValueError(f"{current_path} is a junction but its target is not resolvable. Aborting.")

            current_target = current_target.resolve()
            if not current_target.is_dir():
                log_info(f"JUNCTION: stale current target detected: {current_target}")
            else:
                if current_target.parent != metadata.pkg_path:
                    raise ValueError(
                        f"{current_path} is a junction but its target {current_target} "
                        f"is not under {metadata.pkg_path}. Aborting."
                    )

                current_version = current_target.name
                log_info(f"'current' junction version: {current_version}")
                comparison = JunctionManager.compare_versions(metadata.version_string, current_version)
                # Same-version reinstalls are a supported refresh path. Only
                # keep the existing junction untouched when it points to a
                # newer version and --force was not requested.
                if not force and comparison < 0:
                    log_info(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
                    return False
                if force:
                    log_info(f"JUNCTION: --force: updating current to {metadata.version_string}")
        
        # Refreshing the currently active version may still recreate
        # ``current`` so install can reassert the active package view.
        new_path = current_path.with_name(f"{current_path.name}.__new__.{uuid.uuid4().hex[:8]}")
        old_path = current_path.with_name(f"{current_path.name}.__old__.{uuid.uuid4().hex[:8]}")
        moved_current = False
        try:
            if os.path.lexists(str(new_path)):
                if is_junction(new_path):
                    os.rmdir(str(new_path))
                else:
                    raise RuntimeError(f"Temporary junction path already exists and is unsafe to replace: {new_path}")

            create_junction(new_path, desired_target)
            new_target = get_junction_target(new_path)
            if new_target is None or normalize_path(new_target) != normalize_path(desired_target):
                raise RuntimeError(
                    f"Temporary junction verification failed: expected {desired_target}, got {new_target}"
                )

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

        log_info(f"JUNCTION: created: {current_path.name} -> {desired_target}")
        return True


class ShortcutInstaller:
    """Expand shortcut config and orchestrate Start Menu shortcut creation."""

    @staticmethod
    def _prepare_shortcut(shortcut_spec: ShortcutSpec, metadata: "PackageMetadata") -> PreparedShortcut:
        """Normalize and expand shortcut data before shortcut creation.

        Args:
            shortcut_spec: Canonical shortcut specification from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            A fully expanded :class:`PreparedShortcut` object.
        """

        scope_paths = metadata.scope_paths or compute_scope_paths(metadata.scope)
        metadata.scope_paths = scope_paths
        return prepare_shortcut_spec(shortcut_spec, metadata.identity, scope_paths)

    @staticmethod
    def create_shortcut(shortcut_spec: ShortcutSpec, metadata: "PackageMetadata") -> Tuple[bool, Optional[str]]:
        """Create a single ``.lnk`` shortcut.

        Args:
            shortcut_spec: Canonical shortcut specification from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            ``(True, None)`` on success; otherwise ``(False, error_message)``.
        """

        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_spec, metadata)
            create_shortcut(prepared)
            log_info(f"SHORTCUT: created: {prepared.shortcut_path.name}")
            return True, None
        except Exception as exc:
            name = shortcut_spec.name or "unknown"
            log_error(f"SHORTCUT error creating {name}: {exc}")
            return False, f"Failed to create shortcut '{name}': {exc}"

    @staticmethod
    def install_shortcuts(metadata: "PackageMetadata") -> StepResult:
        """Install every shortcut declared by a package.

        Returns:
            A :class:`StepResult` summarizing the shortcut step.
        """

        result = StepResult(ok=True, changed=False)
        shortcuts = metadata.require_runtime_config().shortcut
        for shortcut_spec in shortcuts:
            ok, error = ShortcutInstaller.create_shortcut(shortcut_spec, metadata)
            if ok:
                result.changed = True
                continue
            result.ok = False
            message = error or f"Failed to create shortcut: {shortcut_spec.name or 'unknown'}"
            log_error(message)
            result.errors.append(message)

        return result


class EnvironmentVariableManager:
    """Expand package config and orchestrate registry-backed env updates."""

    @staticmethod
    def _get_registry_key(scope: Scope) -> Tuple[Any, str]:
        """Return the registry location used for environment updates.

        Args:
            scope: Target installation scope.

        Returns:
            A tuple ``(root_hkey, subkey)``.
        """

        return environment_registry_location(scope)

    @staticmethod
    def broadcast_environment_change() -> None:
        """Notify Windows that environment values changed.

        Returns:
            ``None``. Failures are reported to stdout as warnings because the
            registry write may still have succeeded.
        """

        try:
            broadcast_environment_change()
        except Exception as exc:
            log_warning(f"failed to broadcast environment change notification: {exc}")

    @staticmethod
    def set_environment_variable(name: str, value: str, scope: Scope, expand: bool = True) -> bool:
        """Set one environment variable in the Windows registry.

        Args:
            name: Variable name.
            value: Variable value.
            scope: Target installation scope.
            expand: Whether to store the value as ``REG_EXPAND_SZ``.

        Returns:
            ``True`` on success; ``False`` on failure.
        """

        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            reg = require_winreg()
            reg_type = reg.REG_EXPAND_SZ if expand else reg.REG_SZ
            write_registry_value(root, subkey, name, value, reg_type)
            log_info(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
            EnvironmentVariableManager.broadcast_environment_change()
            return True
        except PermissionError:
            log_error(f"Insufficient permissions to set {scope.value} environment variable: {name}")
            return False
        except Exception as exc:
            log_error(f"ENVIRONMENT error setting {name}: {exc}")
            return False

    @staticmethod
    def install_environment_variables(metadata: "PackageMetadata") -> StepResult:
        """Install every environment variable declared by a package.

        Returns:
            A :class:`StepResult` summarizing the environment-variable step.
        """

        result = StepResult(ok=True, changed=False)
        for env_var in metadata.require_runtime_config().environment:
            name = env_var.name.strip()
            value = env_var.value
            if not name:
                message = f"Environment variable entry is missing Name: {env_var}"
                log_error(message)
                result.ok = False
                result.errors.append(message)
                continue
            expansion = expand_text(str(value), metadata.identity, ExpansionMode.GENERAL)
            if expansion.unresolved:
                unresolved = ", ".join(expansion.unresolved)
                message = f"Environment variable '{name}' contains unresolved variable(s): {unresolved}"
                log_error(message)
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
            log_error(message)
            result.ok = False
            result.errors.append(message)
        return result


class PATHManager:
    """Expand package config and orchestrate registry-backed PATH updates."""

    @staticmethod
    def _path_key(path_value: str) -> str:
        """Normalize a PATH entry for de-duplication.

        Args:
            path_value: Original PATH entry.

        Returns:
            A normalized, case-insensitive comparison key.
        """

        key = os.path.normcase(os.path.normpath(path_value))
        return key.rstrip("\\/")

    @staticmethod
    def get_current_path(scope: Scope) -> List[str]:
        """Read PATH entries from the Windows registry.

        Args:
            scope: Installation scope whose PATH should be read.

        Returns:
            A list of PATH components. Missing values return an empty list.
        """

        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            value, reg_type = read_registry_value(root, subkey, "Path")
            reg = require_winreg()
            if reg_type in (reg.REG_EXPAND_SZ, reg.REG_SZ):
                return [item.strip() for item in str(value).split(";") if item.strip()]
        except FileNotFoundError:
            pass
        except Exception as exc:
            log_error(f"PATH error reading {scope.value} PATH: {exc}")

        return []

    @staticmethod
    def set_path(path_entries: List[str], scope: Scope) -> bool:
        """Write PATH entries to the registry.

        Args:
            path_entries: Ordered PATH components to store.
            scope: Installation scope whose PATH should be updated.

        Returns:
            ``True`` on success; otherwise ``False``.
        """

        try:
            path_value = ";".join(path_entries)
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            reg = require_winreg()
            write_registry_value(root, subkey, "Path", path_value, reg.REG_EXPAND_SZ)
            EnvironmentVariableManager.broadcast_environment_change()
            return True
        except PermissionError:
            log_error(f"Insufficient permissions to set {scope.value} PATH")
            return False
        except Exception as exc:
            log_error(f"PATH error setting {scope.value} PATH: {exc}")
            return False

    @staticmethod
    def add_to_path(new_entries: List[str], metadata: "PackageMetadata") -> StepResult:
        """Append directories to PATH while avoiding duplicates.

        Returns:
            A :class:`StepResult` summarizing the PATH update.
        """

        result = StepResult(ok=True, changed=False)
        valid_entries: List[str] = []

        for entry in new_entries:
            expansion = expand_text(str(entry), metadata.identity, ExpansionMode.GENERAL)
            if expansion.unresolved:
                unresolved = ", ".join(expansion.unresolved)
                message = f"PATH entry '{entry}' contains unresolved variable(s): {unresolved}"
                log_error(message)
                result.ok = False
                result.errors.append(message)
                continue

            expanded = expansion.value.strip()
            if expanded == "":
                message = f"PATH entry '{entry}' expands to an empty value and will not be added."
                log_error(message)
                result.ok = False
                result.errors.append(message)
                continue

            normalized = os.path.normpath(expanded)
            if normalized == "":
                message = f"PATH entry '{entry}' normalized to an empty value and will not be added."
                log_error(message)
                result.ok = False
                result.errors.append(message)
                continue

            valid_entries.append(normalized)

        if not valid_entries:
            return result if result.errors else StepResult(ok=True, changed=False)

        current_path = PATHManager.get_current_path(metadata.scope)
        updated_path = current_path.copy()
        existing_keys = {PATHManager._path_key(item) for item in current_path if item}
        added_entries: List[str] = []

        for entry in valid_entries:
            key = PATHManager._path_key(entry)
            if key not in existing_keys:
                updated_path.append(entry)
                existing_keys.add(key)
                added_entries.append(entry)
                log_info(f"PATH: adding to {metadata.scope.value} scope: {entry}")

        if not added_entries:
            return result

        if PATHManager.set_path(updated_path, metadata.scope):
            result.changed = True
            return result

        message = f"Failed to update {metadata.scope.value} PATH."
        log_error(message)
        result.ok = False
        result.errors.append(message)
        return result

    @staticmethod
    def _system_drive_root() -> str:
        """Return the system drive root, defaulting to ``C:\\``.

        Returns:
            A drive-root string ending with a trailing backslash.
        """

        return system_drive_root()

    @staticmethod
    def ensure_bin_in_path(metadata: "PackageMetadata") -> StepResult:
        """Ensure the per-scope ``bin`` directory exists and is on PATH.

        Returns:
            A :class:`StepResult` summarizing the bin-directory and PATH work.
        """

        scope_paths = metadata.scope_paths or compute_scope_paths(metadata.scope)
        metadata.scope_paths = scope_paths
        bin_dir = scope_paths.bin_dir

        changed = False
        try:
            existed_before = bin_dir.exists()
            bin_dir.mkdir(parents=True, exist_ok=True)
            changed = not existed_before
        except OSError as exc:
            return StepResult(ok=False, errors=[f"Failed to create bin directory {bin_dir}: {exc}"])

        current_path = PATHManager.get_current_path(metadata.scope)
        bin_dir_str = str(bin_dir)
        bin_key = PATHManager._path_key(bin_dir_str)
        current_keys = {PATHManager._path_key(item) for item in current_path if item}
        if bin_key not in current_keys:
            path_result = PATHManager.add_to_path([bin_dir_str], metadata)
            path_result.changed = path_result.changed or changed
            return path_result

        return StepResult(ok=True, changed=changed)


class BinFileCreator:
    """Expand wrapper config and orchestrate writes to the scope ``bin`` directory."""

    @staticmethod
    def get_bin_dir(scope: Scope) -> Path:
        """Return the scope-specific ``bin`` directory.

        Args:
            scope: Installation scope for which to compute the bin directory.

        Returns:
            The path to the user or machine bin directory.
        """

        if scope == Scope.USER:
            return Path.home() / "bin"
        return Path(PATHManager._system_drive_root()) / "bin"

    @staticmethod
    def create_wrapper(wrapper_spec: BinSpec, metadata: "PackageMetadata") -> Tuple[bool, Optional[str]]:
        """Create one wrapper file.

        Args:
            wrapper_spec: Canonical wrapper specification from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            ``(True, None)`` when the wrapper changed, ``(True, "unchanged")``
            when it was already up to date, or ``(False, error_message)`` on
            failure.
        """

        try:
            raw_name = wrapper_spec.name
            raw_content = wrapper_spec.content
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
            _warn_if_output_path_is_unusual("bin", bin_dir, expanded_name, wrapper_path)
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)

            extension = wrapper_path.suffix.lower()
            if extension in (".cmd", ".bat"):
                try:
                    desired_bytes = expanded_content.encode("ascii")
                except UnicodeEncodeError:
                    log_warning(
                        f"non-ASCII content in {extension} wrapper; writing UTF-8 with BOM: {wrapper_path.name}"
                    )
                    desired_bytes = expanded_content.encode("utf-8-sig")
            else:
                desired_bytes = expanded_content.encode("utf-8")

            existed_before = wrapper_path.exists()
            if existed_before:
                try:
                    if wrapper_path.read_bytes() == desired_bytes:
                        log_info(f"BIN: up-to-date: {wrapper_path}")
                        return True, "unchanged"
                except OSError:
                    pass

            write_bytes_atomic(wrapper_path, desired_bytes)
            action = "updated" if existed_before else "created"
            log_info(f"BIN: {action}: {wrapper_path}")
            return True, None
        except Exception as exc:
            name = wrapper_spec.name or "unknown"
            log_error(f"BIN error creating {name}: {exc}")
            return False, f"Failed to create wrapper '{name}': {exc}"

    @staticmethod
    def install_wrappers(metadata: "PackageMetadata") -> StepResult:
        """Install every wrapper declared by a package.

        Returns:
            A :class:`StepResult` summarizing the wrapper-install step.
        """

        result = StepResult(ok=True, changed=False)
        for wrapper_spec in metadata.require_runtime_config().bin:
            ok, error = BinFileCreator.create_wrapper(wrapper_spec, metadata)
            if ok:
                if error != "unchanged":
                    result.changed = True
                continue
            message = error or f"Failed to create wrapper: {wrapper_spec.name or 'unknown'}"
            log_error(message)
            result.ok = False
            result.errors.append(message)
        return result


def install_shortcuts_step(metadata: "PackageMetadata") -> StepResult:
    """Run the shortcut-install step for one package.

    Returns:
        A :class:`StepResult` describing the shortcut step outcome.
    """

    if not metadata.require_runtime_config().shortcut:
        return StepResult(ok=True, changed=False)
    log_info("")
    log_info("Creating shortcuts...")
    return ShortcutInstaller.install_shortcuts(metadata)


def install_environment_variables_step(metadata: "PackageMetadata") -> StepResult:
    """Run the environment-variable step for one package.

    Returns:
        A :class:`StepResult` describing the environment-variable step outcome.
    """

    if not metadata.require_runtime_config().environment:
        return StepResult(ok=True, changed=False)
    log_info("")
    log_info("Setting environment variables...")
    return EnvironmentVariableManager.install_environment_variables(metadata)


def ensure_bin_in_path_step(metadata: "PackageMetadata") -> StepResult:
    """Run the scope ``bin`` directory/PATH bootstrap step.

    Returns:
        A :class:`StepResult` describing the bin-directory/PATH step outcome.
    """

    log_info("")
    log_info("Managing PATH...")
    return PATHManager.ensure_bin_in_path(metadata)


def install_extra_path_entries_step(metadata: "PackageMetadata") -> StepResult:
    """Run the package-specific extra PATH entry step.

    Returns:
        A :class:`StepResult` describing the extra PATH entry step outcome.
    """

    extra_entries = metadata.require_runtime_config().path
    if not extra_entries:
        return StepResult(ok=True, changed=False)
    return PATHManager.add_to_path(extra_entries, metadata)


def install_wrappers_step(metadata: "PackageMetadata") -> StepResult:
    """Run the wrapper-install step for one package.

    Returns:
        A :class:`StepResult` describing the wrapper step outcome.
    """

    if not metadata.require_runtime_config().bin:
        return StepResult(ok=True, changed=False)
    log_info("")
    log_info("Creating executable wrappers...")
    return BinFileCreator.install_wrappers(metadata)


INSTALL_STEPS: Tuple[InstallStep, ...] = (
    install_shortcuts_step,
    install_environment_variables_step,
    ensure_bin_in_path_step,
    install_extra_path_entries_step,
    install_wrappers_step,
)




EXTENDED_HELP = r"""
Extended help
-------------

Quick start
~~~~~~~~~~~

Notes:
  - ``pkg --help`` and ``pkg --version`` do not write files.
  - ``Install`` does not auto-create ``pkg.toml``.
  - ``UpdateConfig`` creates a documented starter template when ``pkg.toml`` is missing.
  - ``UpdateConfig`` syncs only canonical top-level metadata keys in an existing file.
  - Architecture and developer docs live in ``docs/architecture.md`` and ``docs/api.md``.

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
  - PATH/env:  HKCU\Environment
  - bin dir:   %USERPROFILE%\bin

Machine scope (requires admin):
  - shortcuts: %PROGRAMDATA%\Microsoft\Windows\Start Menu\opt
  - PATH/env:  HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment
  - bin dir:   <SYSTEMDRIVE>\bin

Canonical config keys
~~~~~~~~~~~~~~~~~~~~~

1) Shortcuts (``shortcut`` list)
   Supported keys:

   - ``name`` (required): output name after expansion; may be a simple name,
     a nested relative path under the default shortcut root, or a path-like
     destination that resolves outside that root
   - ``targetPath`` (required): executable path
   - ``arguments`` (optional)
   - ``workingDirectory`` (optional)
   - ``iconLocation`` (optional): e.g. ``C:\path\icon.ico,0``
   - ``description`` (optional)

2) Environment variables (``environment`` list)
   Canonical keys are ``Name`` and ``Value``.

3) PATH additions (``path`` list)
   Use repeated ``[[path]]`` tables with the single key ``value``.

4) Bin wrappers (``bin`` list)
   Each entry has ``name`` and ``content``. ``name`` follows the same output
   placement rule as shortcuts: it may be a simple name, a nested relative
   path, or a path-like destination outside the default bin root. Wrapper
   content uses the script expansion mode so PowerShell variables such as
   ``$PSScriptRoot`` remain literal unless they are package variables.

Output placement notes
~~~~~~~~~~~~~~~~~~~~~~

- ``shortcut.name`` and ``bin.name`` are expanded before placement.
- Nested relative paths inside the default scope root are allowed.
- Absolute paths and escaping parent traversal are also allowed.
- When the final destination lands outside the default shortcut/bin root,
  install prints a warning but still creates the output.

Bootstrap interpreter selection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``pkg.cmd`` chooses Python in this order:

1. ``--python <exe-or-command>``
2. ``PKG_PYTHON``
3. ``pkg.python`` next to ``pkg.cmd``
4. ``python`` from ``PATH``

``pkg.py`` intentionally accepts the hidden ``--python`` argument so the
launcher can forward it unchanged.

Variable expansion
~~~~~~~~~~~~~~~~~~

- ``$App``, ``$Icons``, and ``$Shortcuts`` expand everywhere.
- ``${VAR}`` expands everywhere and must resolve.
- plain non-package ``$NAME`` tokens are treated as unresolved in regular
  fields and remain literal inside wrapper content.
"""

CANONICAL_TOP_LEVEL_KEYS = {
    "name",
    "version",
    "localVersion",
    "description",
    "homepage",
    "downloadURL",
    "only_portable",
    "environment",
    "shortcut",
    "path",
    "bin",
}
LEGACY_TOP_LEVEL_KEY_HINTS: Dict[str, Optional[str]] = {
    "env": "environment",
    "shortcuts": "shortcut",
    "portable": "only_portable",
    "onlyportable": "only_portable",
    "local_version": "localVersion",
    "download_url": "downloadURL",
    "main": None,
}
OWNED_METADATA_FIELDS = ("name", "version", "localVersion", "only_portable")
_CANONICAL_SHORTCUT_KEYS = {"name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description"}
_SHORTCUT_LEGACY_KEY_HINTS = {
    "path": "targetPath",
    "target_path": "targetPath",
    "args": "arguments",
    "workdir": "workingDirectory",
    "working_directory": "workingDirectory",
    "icon_location": "iconLocation",
    "desc": "description",
}
_CANONICAL_ENVIRONMENT_KEYS = {"Name", "Value"}
_ENVIRONMENT_LEGACY_KEY_HINTS = {"name": "Name", "value": "Value"}
_CANONICAL_BIN_KEYS = {"name", "content"}
_CANONICAL_PATH_KEYS = {"value"}
_PATH_LEGACY_KEY_HINTS = {"path": "value"}


def _render_allowed_keys(keys: List[str]) -> str:
    """Render a stable comma-separated list of allowed key names.

    Args:
        keys: Ordered key names to include.

    Returns:
        A human-readable comma-separated string.
    """

    return ", ".join(keys)


def _validate_exact_keys(
    data: Dict[str, Any],
    *,
    allowed: set[str],
    context: str,
    ordered_allowed: List[str],
    legacy_hints: Optional[Dict[str, Optional[str]]] = None,
) -> None:
    """Validate that a mapping uses only canonical keys.

    Args:
        data: Mapping to validate.
        allowed: Canonical keys accepted in *context*.
        context: Human-readable location such as ``config`` or ``shortcut[0]``.
        ordered_allowed: Stable ordered list of canonical keys for error text.
        legacy_hints: Optional mapping from lower-cased legacy spellings to the
            canonical replacement, or ``None`` for special cases with no direct
            replacement.

    Raises:
        ConfigValidationError: If *data* contains an unknown or legacy key.
    """

    for key in data.keys():
        if key in allowed:
            continue
        hint = None
        if legacy_hints is not None:
            hint = legacy_hints.get(str(key).lower())
            if str(key).lower() in legacy_hints:
                if hint is None:
                    raise ConfigValidationError(
                        f"Unsupported legacy key '{key}' in {context}. Use canonical top-level metadata keys instead of [[main]]."
                    )
                raise ConfigValidationError(
                    f"Unsupported legacy key '{key}' in {context}. Use '{hint}' instead."
                )
        raise ConfigValidationError(
            f"Unknown key '{key}' in {context}. Allowed keys: {_render_allowed_keys(ordered_allowed)}"
        )


def _normalize_optional_string(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize an optional string field.

    Args:
        value: Raw value to normalize.
        field_name: Human-readable field name for error messages.

    Returns:
        ``None`` when *value* is ``None``; otherwise the normalized string.

    Raises:
        ConfigValidationError: If *value* is not a string or ``None``.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigValidationError(f"'{field_name}' must be a string, got: {type(value).__name__}")
    return value


def _normalize_required_string(value: Any, *, field_name: str) -> str:
    """Normalize a required-or-empty string field.

    Args:
        value: Raw value to normalize.
        field_name: Human-readable field name for error messages.

    Returns:
        A string value, or ``""`` when *value* is missing.

    Raises:
        ConfigValidationError: If *value* is present but not a string.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigValidationError(f"'{field_name}' must be a string, got: {type(value).__name__}")
    return value


def _normalize_local_version_value(value: Any, *, field_name: str) -> int:
    """Normalize the ``localVersion`` scalar.

    Args:
        value: Raw value to normalize.
        field_name: Human-readable field name for error messages.

    Returns:
        The normalized integer local version.

    Raises:
        ConfigValidationError: If *value* is not an integer or digit string.
    """

    if isinstance(value, bool):
        raise ConfigValidationError(f"'{field_name}' must be an integer, got: {type(value).__name__}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ConfigValidationError(f"'{field_name}' must be an integer, got: {type(value).__name__}")


def _normalize_only_portable_value(value: Any, *, field_name: str) -> bool:
    """Normalize the ``only_portable`` scalar.

    Args:
        value: Raw value to normalize.
        field_name: Human-readable field name for error messages.

    Returns:
        The normalized boolean value.

    Raises:
        ConfigValidationError: If *value* is not a boolean.
    """

    if not isinstance(value, bool):
        raise ConfigValidationError(f"'{field_name}' must be a boolean, got: {type(value).__name__}")
    return value


def _normalize_bin_content(value: Any) -> str:
    """Normalize wrapper content so escaped newlines become real newlines.

    Args:
        value: Raw wrapper content value from the config.

    Returns:
        Wrapper content with ``\n`` and ``\r\n`` escapes expanded when the
        source contains no actual newline characters.
    """

    text = _normalize_required_string(value, field_name="bin.content")
    if "\n" in text:
        return text
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def _normalize_environment_entries(raw: Any) -> List[EnvVarSpec]:
    """Normalize ``[[environment]]`` entries.

    Args:
        raw: Parsed TOML value for the ``environment`` key.

    Returns:
        A list of :class:`EnvVarSpec` objects.

    Raises:
        ConfigValidationError: If *raw* is not a canonical environment table list.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigValidationError(f"'environment' must be a list, got: {type(raw).__name__}")
    result: List[EnvVarSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"'environment[{index}]' must be a table, got: {type(item).__name__}")
        _validate_exact_keys(
            item,
            allowed=_CANONICAL_ENVIRONMENT_KEYS,
            context=f"environment[{index}]",
            ordered_allowed=["Name", "Value"],
            legacy_hints=_ENVIRONMENT_LEGACY_KEY_HINTS,
        )
        result.append(
            EnvVarSpec(
                name=_normalize_required_string(item.get("Name"), field_name=f"environment[{index}].Name"),
                value=_normalize_required_string(item.get("Value"), field_name=f"environment[{index}].Value"),
            )
        )
    return result


def _normalize_shortcut_entries(raw: Any) -> List[ShortcutSpec]:
    """Normalize ``[[shortcut]]`` entries.

    Args:
        raw: Parsed TOML value for the ``shortcut`` key.

    Returns:
        A list of :class:`ShortcutSpec` objects.

    Raises:
        ConfigValidationError: If *raw* is not a canonical shortcut table list.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigValidationError(f"'shortcut' must be a list, got: {type(raw).__name__}")
    result: List[ShortcutSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"'shortcut[{index}]' must be a table, got: {type(item).__name__}")
        _validate_exact_keys(
            item,
            allowed=_CANONICAL_SHORTCUT_KEYS,
            context=f"shortcut[{index}]",
            ordered_allowed=["name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description"],
            legacy_hints=_SHORTCUT_LEGACY_KEY_HINTS,
        )
        result.append(
            ShortcutSpec(
                name=_normalize_required_string(item.get("name"), field_name=f"shortcut[{index}].name"),
                target_path=_normalize_required_string(item.get("targetPath"), field_name=f"shortcut[{index}].targetPath"),
                arguments=_normalize_required_string(item.get("arguments"), field_name=f"shortcut[{index}].arguments"),
                working_directory=_normalize_required_string(
                    item.get("workingDirectory"),
                    field_name=f"shortcut[{index}].workingDirectory",
                ),
                icon_location=_normalize_required_string(item.get("iconLocation"), field_name=f"shortcut[{index}].iconLocation"),
                description=_normalize_required_string(item.get("description"), field_name=f"shortcut[{index}].description"),
            )
        )
    return result


def _normalize_bin_entries(raw: Any) -> List[BinSpec]:
    """Normalize ``[[bin]]`` entries.

    Args:
        raw: Parsed TOML value for the ``bin`` key.

    Returns:
        A list of :class:`BinSpec` objects.

    Raises:
        ConfigValidationError: If *raw* is not a canonical bin table list.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigValidationError(f"'bin' must be a list, got: {type(raw).__name__}")
    result: List[BinSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"'bin[{index}]' must be a table, got: {type(item).__name__}")
        _validate_exact_keys(
            item,
            allowed=_CANONICAL_BIN_KEYS,
            context=f"bin[{index}]",
            ordered_allowed=["name", "content"],
        )
        result.append(
            BinSpec(
                name=_normalize_required_string(item.get("name"), field_name=f"bin[{index}].name"),
                content=_normalize_bin_content(item.get("content")),
            )
        )
    return result


def _normalize_path_entries(raw: Any) -> List[str]:
    """Normalize ``[[path]]`` entries.

    Args:
        raw: Parsed TOML value for the ``path`` key.

    Returns:
        A list of PATH strings.

    Raises:
        ConfigValidationError: If *raw* is not a canonical path table list.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigValidationError(f"'path' must be a list of [[path]] tables, got: {type(raw).__name__}")
    result: List[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"'path[{index}]' must be a table, got: {type(item).__name__}")
        _validate_exact_keys(
            item,
            allowed=_CANONICAL_PATH_KEYS,
            context=f"path[{index}]",
            ordered_allowed=["value"],
            legacy_hints=_PATH_LEGACY_KEY_HINTS,
        )
        if "value" not in item:
            raise ConfigValidationError(f"'path[{index}]' is missing required key: value")
        value = item.get("value")
        if not isinstance(value, str):
            raise ConfigValidationError(f"'path[{index}].value' must be a string, got: {type(value).__name__}")
        result.append(value)
    return result


def normalize_runtime_config(raw: Any, identity: PackageIdentity) -> PackageConfig:
    """Normalize raw config data into the runtime model.

    Args:
        raw: Parsed TOML data or ``None``.
        identity: Directory-derived package identity used to supply defaults.

    Returns:
        A fully normalized :class:`PackageConfig` object.

    Raises:
        ConfigValidationError: If *raw* is not a canonical configuration table.
    """

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"Configuration must be a TOML table, got: {type(raw).__name__}")

    _validate_exact_keys(
        raw,
        allowed=CANONICAL_TOP_LEVEL_KEYS,
        context="config",
        ordered_allowed=[
            "name",
            "version",
            "localVersion",
            "description",
            "homepage",
            "downloadURL",
            "only_portable",
            "environment",
            "shortcut",
            "path",
            "bin",
        ],
        legacy_hints=LEGACY_TOP_LEVEL_KEY_HINTS,
    )

    name = _normalize_optional_string(raw.get("name"), field_name="name")
    version = _normalize_optional_string(raw.get("version"), field_name="version")
    local_version = raw.get("localVersion")
    if local_version is None:
        normalized_local_version = identity.local_version
    else:
        normalized_local_version = _normalize_local_version_value(local_version, field_name="localVersion")
    only_portable_value = raw.get("only_portable")
    normalized_only_portable = (
        identity.only_portable_by_name
        if only_portable_value is None
        else _normalize_only_portable_value(only_portable_value, field_name="only_portable")
    )

    return PackageConfig(
        description=_normalize_optional_string(raw.get("description"), field_name="description"),
        homepage=_normalize_optional_string(raw.get("homepage"), field_name="homepage"),
        download_url=_normalize_optional_string(raw.get("downloadURL"), field_name="downloadURL"),
        only_portable=normalized_only_portable,
        environment=_normalize_environment_entries(raw.get("environment")),
        shortcut=_normalize_shortcut_entries(raw.get("shortcut")),
        path=_normalize_path_entries(raw.get("path")),
        bin=_normalize_bin_entries(raw.get("bin")),
    )


def validate_runtime_config(config: PackageConfig) -> None:
    """Validate required fields in a normalized runtime config.

    Args:
        config: Runtime config to validate.

    Raises:
        ConfigValidationError: If required fields are missing.
    """

    errors: List[str] = []
    for index, shortcut in enumerate(config.shortcut):
        missing = []
        if not shortcut.name.strip():
            missing.append("name")
        if not shortcut.target_path.strip():
            missing.append("targetPath")
        if missing:
            errors.append(f"shortcut[{index}] missing required key(s): {', '.join(missing)}")
    for index, env in enumerate(config.environment):
        missing = []
        if not env.name.strip():
            missing.append("Name")
        if env.value == "":
            missing.append("Value")
        if missing:
            errors.append(f"environment[{index}] missing required key(s): {', '.join(missing)}")
    for index, wrapper in enumerate(config.bin):
        missing = []
        if not wrapper.name.strip():
            missing.append("name")
        if wrapper.content == "":
            missing.append("content")
        if missing:
            errors.append(f"bin[{index}] missing required key(s): {', '.join(missing)}")
    if errors:
        joined = "\n  - " + "\n  - ".join(errors)
        raise ConfigValidationError(f"Invalid configuration:{joined}")


def check_metadata_consistency(identity: PackageIdentity, raw_config: Dict[str, Any]) -> List[str]:
    """Compare directory-derived metadata with raw configuration metadata.

    Args:
        identity: Package identity derived from the directory layout.
        raw_config: Raw config dictionary derived from ``pkg.toml``.

    Returns:
        A list of human-readable mismatch descriptions. The list is empty when
        the metadata is consistent.

    Raises:
        TypeError: If *raw_config* is not a dictionary.
    """

    if not isinstance(raw_config, dict):
        raise TypeError("raw_config must be a dict")

    inconsistencies: List[str] = []
    if "name" in raw_config and raw_config.get("name") not in (None, ""):
        if _normalize_optional_string(raw_config.get("name"), field_name="name") != identity.name:
            inconsistencies.append(f"Name mismatch: directory='{identity.name}', config='{raw_config.get('name')}'")
    if "version" in raw_config and raw_config.get("version") not in (None, ""):
        if _normalize_optional_string(raw_config.get("version"), field_name="version") != identity.version:
            inconsistencies.append(f"Version mismatch: directory='{identity.version}', config='{raw_config.get('version')}'")
    if "localVersion" in raw_config and raw_config.get("localVersion") not in (None, ""):
        normalized_local_version = _normalize_local_version_value(raw_config.get("localVersion"), field_name="localVersion")
        if normalized_local_version != identity.local_version:
            inconsistencies.append(
                f"LocalVersion mismatch: directory='{identity.local_version}', config='{raw_config.get('localVersion')}'"
            )
    if "only_portable" in raw_config and raw_config.get("only_portable") is not None:
        normalized_only_portable = _normalize_only_portable_value(
            raw_config.get("only_portable"),
            field_name="only_portable",
        )
        if normalized_only_portable != identity.only_portable_by_name:
            inconsistencies.append(
                f"Portable flag mismatch: directory='{identity.only_portable_by_name}', config='{normalized_only_portable}'"
            )
    return inconsistencies


def read_runtime_config(identity: PackageIdentity, use_defaults: bool = False) -> Tuple[PackageConfig, Dict[str, Any], List[str]]:
    """Read and validate ``pkg.toml`` for one package version.

    Args:
        identity: Package identity whose ``pkg.toml`` should be loaded.
        use_defaults: Whether to fall back to defaults when parsing or
            validation fails.

    Returns:
        A tuple ``(runtime_config, raw_dict, warnings)`` where ``raw_dict`` is
        the file-authored config when ``pkg.toml`` exists, or ``{}`` when no
        config is available.

    Raises:
        ConfigValidationError: If the config is invalid and *use_defaults* is
            ``False``.
        RuntimeError: If the config cannot be read and *use_defaults* is
            ``False``.
    """

    warnings: List[str] = []
    toml_path = identity.version_path / "pkg.toml"
    if toml_path.exists():
        try:
            loaded = read_toml_file(toml_path)
            config = normalize_runtime_config(loaded, identity)
            validate_runtime_config(config)
            return config, dict(loaded), warnings
        except ConfigValidationError:
            raise
        except Exception as exc:
            if not use_defaults:
                raise RuntimeError(f"Error loading TOML config from {toml_path}: {exc}") from exc
            warnings.append(f"Error loading TOML config from {toml_path}: {exc}")
            warnings.append("Proceeding with defaults because --use-defaults was provided.")
    config = normalize_runtime_config({}, identity)
    validate_runtime_config(config)
    warnings.append(f"No pkg.toml found at {toml_path}; using defaults without creating a file.")
    return config, {}, warnings


class PackageMetadata:
    """Package identity, scope, and runtime config for one package version."""

    def __init__(self, version_path: Path):
        """Create package metadata for a concrete version directory.

        Args:
            version_path: Path pointing at a version directory, the package root,
                or the ``current`` junction.
        """

        resolved = resolve_input_path(Path(version_path))
        self.resolved_input = resolved
        self.identity = PackageIdentity.from_resolved_input(resolved)
        self.scope: Scope = Scope.USER
        self.scope_paths: Optional[ScopePaths] = None
        self.runtime_config: Optional[PackageConfig] = None

    @property
    def version_path(self) -> Path:
        """Return the concrete version directory path.

        Returns:
            The package version directory.
        """

        return self.identity.version_path

    @property
    def pkg_path(self) -> Path:
        """Return the package root directory.

        Returns:
            The directory that owns ``current`` and version folders.
        """

        return self.identity.package_root

    @property
    def name(self) -> str:
        """Return the directory-derived package name.

        Returns:
            The package name.
        """

        return self.identity.name

    @property
    def version_string(self) -> str:
        """Return the version-directory name.

        Returns:
            The full version-directory name such as ``v1.2.3.l4``.
        """

        return self.identity.version_string

    @property
    def is_current(self) -> bool:
        """Return whether this version already matches ``current``.

        Returns:
            ``True`` when the version is current.
        """

        return self.identity.is_current

    @property
    def only_portable(self) -> bool:
        """Return the effective package portability flag.

        Returns:
            ``True`` when the package must stay portable.
        """

        if self.runtime_config is None:
            return self.identity.only_portable_by_name
        return self.runtime_config.only_portable

    def require_runtime_config(self) -> PackageConfig:
        """Return the loaded runtime config or raise a clear error.

        Returns:
            The normalized runtime config.

        Raises:
            RuntimeError: If :meth:`load_config` has not run yet.
        """

        if self.runtime_config is None:
            raise RuntimeError("Runtime config has not been loaded yet.")
        return self.runtime_config

    def load_config(self, *, use_defaults: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        """Load and normalize the package runtime configuration.

        Args:
            use_defaults: Whether defaults may be used when TOML loading fails.

        Returns:
            A tuple ``(raw_config_dict, warnings)``.
        """

        config, raw_data, warnings = read_runtime_config(self.identity, use_defaults=use_defaults)
        self.runtime_config = config
        return raw_data, warnings

    def update_config(self) -> StepResult:
        """Synchronize owned metadata fields back to ``pkg.toml``.

        Returns:
            A :class:`StepResult` describing the update. Missing configs are
            created as documented starter templates. Existing configs are
            updated only when they already use canonical top-level metadata keys.
        """

        toml_path = self.version_path / "pkg.toml"

        if not toml_path.exists():
            rendered = create_starter_config(self.identity)
            write_text_atomic(toml_path, rendered, backup=False)
            log_info(f"Created: {toml_path}")
            return StepResult(ok=True, changed=True)

        original_text = toml_path.read_text(encoding="utf-8")
        rendered, changed = sync_config_metadata_text(original_text, self.identity)
        if not changed or rendered == original_text:
            log_info(f"Configuration already up to date: {toml_path}")
            return StepResult(ok=True, changed=False)

        write_text_atomic(toml_path, rendered, backup=True)
        log_info(f"Updated: {toml_path}")
        return StepResult(ok=True, changed=True)

    def set_scope(self, scope: Scope) -> None:
        """Store the install scope and precompute scope-specific paths.

        Args:
            scope: Selected installation scope.
        """

        self.scope = scope
        self.scope_paths = compute_scope_paths(scope)


def _to_toml_scalar(value: Any) -> str:
    """Render a Python value as TOML literal text.

    Args:
        value: Python scalar or list value.

    Returns:
        TOML literal text representing *value*.
    """

    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_to_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def metadata_sync_payload(identity: PackageIdentity) -> Dict[str, Any]:
    """Return the directory-derived metadata owned by ``pkg``.

    Args:
        identity: Package identity whose metadata should be serialized.

    Returns:
        Dictionary containing only metadata fields that ``pkg`` owns.
    """

    return {
        "name": identity.name,
        "version": identity.version,
        "localVersion": identity.local_version,
        "only_portable": identity.only_portable_by_name,
    }


def _parse_editable_top_level_metadata_line(line: str) -> Tuple[str, str, str, str]:
    """Parse one editable top-level metadata line.

    The helper is intentionally narrow: it supports the single-line assignment
    shape that ``UpdateConfig`` rewrites safely and understands ``#`` only when
    it appears outside quoted strings.
    """

    index = 0
    while index < len(line) and line[index].isspace():
        index += 1
    indent = line[:index]

    key_start = index
    if key_start >= len(line) or not (line[key_start].isalpha() or line[key_start] == "_"):
        raise ConfigValidationError("pkg.toml contains a metadata line that UpdateConfig cannot rewrite safely. Edit the line manually.")
    index += 1
    while index < len(line) and (line[index].isalnum() or line[index] == "_"):
        index += 1
    key = line[key_start:index]

    while index < len(line) and line[index] in " \t":
        index += 1
    if index >= len(line) or line[index] != "=":
        raise ConfigValidationError(f"pkg.toml metadata line for '{key}' cannot be updated safely. Edit the line manually.")
    index += 1
    while index < len(line) and line[index] in " \t":
        index += 1
    value_start = index

    in_basic_string = False
    in_literal_string = False
    escaped = False

    while index < len(line):
        char = line[index]
        if in_basic_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_basic_string = False
            index += 1
            continue

        if in_literal_string:
            if char == "'":
                in_literal_string = False
            index += 1
            continue

        if char == "#":
            break
        if char == '"':
            if line.startswith('"""', index):
                raise ConfigValidationError(
                    f"pkg.toml metadata line for '{key}' cannot be updated safely because multi-line strings are not supported here. Edit the line manually."
                )
            in_basic_string = True
        elif char == "'":
            if line.startswith("'''", index):
                raise ConfigValidationError(
                    f"pkg.toml metadata line for '{key}' cannot be updated safely because multi-line strings are not supported here. Edit the line manually."
                )
            in_literal_string = True
        index += 1

    if in_basic_string or in_literal_string or escaped:
        raise ConfigValidationError(
            f"pkg.toml metadata line for '{key}' cannot be updated safely because the value is not a supported single-line scalar. Edit the line manually."
        )

    raw_value = line[value_start:index]
    value_text = raw_value.rstrip(" \t")
    comment_text = raw_value[len(value_text):] + line[index:]
    if value_text == "":
        raise ConfigValidationError(f"pkg.toml metadata line for '{key}' is missing a value. Edit the line manually.")

    return indent, key, value_text, comment_text


def sync_config_metadata_text(text: str, identity: PackageIdentity) -> Tuple[str, bool]:
    """Synchronize owned metadata directly in canonical ``pkg.toml`` text.

    Args:
        text: Existing TOML text to update.
        identity: Package identity that supplies the target metadata values.

    Returns:
        Tuple ``(rendered_text, changed)``.

    Raises:
        ConfigValidationError: If the existing file is not valid TOML or still
            uses legacy top-level metadata spellings.
    """

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigValidationError(
            f"pkg.toml is structurally invalid and cannot be updated safely: {exc}. Edit the config manually."
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigValidationError("pkg.toml must contain a top-level TOML table.")

    _validate_exact_keys(
        parsed,
        allowed=CANONICAL_TOP_LEVEL_KEYS,
        context="config",
        ordered_allowed=list(CANONICAL_TOP_LEVEL_KEYS),
        legacy_hints=LEGACY_TOP_LEVEL_KEY_HINTS,
    )

    metadata = metadata_sync_payload(identity)
    lines = text.splitlines(keepends=True)
    key_pattern = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

    in_table = False
    first_table_index = len(lines)
    line_indexes: Dict[str, int] = {}
    insert_after = -1

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[[main]]"):
            raise ConfigValidationError(
                "Unsupported legacy key 'main' in config. Use canonical top-level metadata keys instead of [[main]]."
            )
        if stripped.startswith("["):
            if first_table_index == len(lines):
                first_table_index = index
            in_table = True
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if in_table:
            continue
        match = key_pattern.match(line.rstrip("\r\n"))
        if match is None:
            continue
        key = match.group("key")
        if key in metadata:
            _parse_editable_top_level_metadata_line(line.rstrip("\r\n"))
            if key in line_indexes:
                raise ConfigValidationError(f"Duplicate metadata key '{key}' in config.")
            line_indexes[key] = index
            insert_after = max(insert_after, index)
            continue
        lower = key.lower()
        if lower in LEGACY_TOP_LEVEL_KEY_HINTS:
            hint = LEGACY_TOP_LEVEL_KEY_HINTS[lower]
            if hint is None:
                raise ConfigValidationError(
                    f"Unsupported legacy key '{key}' in config. Use canonical top-level metadata keys instead of [[main]]."
                )
            raise ConfigValidationError(f"Unsupported legacy key '{key}' in config. Use '{hint}' instead.")

    changed = False
    for key, value in metadata.items():
        rendered_value = _to_toml_scalar(value)
        line_index = line_indexes.get(key)
        if line_index is not None:
            line = lines[line_index].rstrip("\r\n")
            indent, _, existing_value, comment_text = _parse_editable_top_level_metadata_line(line)
            if existing_value != rendered_value:
                line_ending = "\n"
                if lines[line_index].endswith("\r\n"):
                    line_ending = "\r\n"
                lines[line_index] = f"{indent}{key} = {rendered_value}{comment_text}{line_ending}"
                changed = True
            continue
        insert_index = first_table_index if first_table_index != len(lines) else len(lines)
        new_line = f"{key} = {rendered_value}\n"
        if insert_after >= 0:
            insert_index = insert_after + 1
            insert_after += 1
        elif insert_index == len(lines):
            if lines and lines[-1].strip() != "":
                lines.append("\n")
                insert_index = len(lines)
            insert_after = insert_index
        lines.insert(insert_index, new_line)
        if first_table_index != len(lines) and insert_index <= first_table_index:
            first_table_index += 1
        changed = True

    rendered = "".join(lines)
    try:
        reparsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigValidationError(
            f"pkg.toml metadata rewrite produced invalid TOML and was aborted: {exc}. Edit the config manually."
        ) from exc
    if not isinstance(reparsed, dict):
        raise ConfigValidationError("pkg.toml metadata rewrite produced an invalid top-level structure.")

    return rendered, changed


def create_starter_config(identity: PackageIdentity) -> str:
    """Create a documented starter ``pkg.toml`` for a package.

    Args:
        identity: Package identity that supplies starter metadata values.

    Returns:
        TOML text that includes synchronized metadata, documentation comments,
        and commented example blocks for the supported runtime sections.
    """

    metadata = metadata_sync_payload(identity)

    example_exe = re.sub(r"[^A-Za-z0-9._-]+", "", identity.name) or "App"
    example_env = re.sub(r"[^A-Za-z0-9]+", "_", identity.name).strip("_").upper() or "APP"
    example_wrapper = re.sub(r"[^A-Za-z0-9]+", "-", identity.name).strip("-").lower() or "app"
    example_description = ""
    example_homepage = "https://"
    example_download = "https://"
    example_exe_path = rf"$App\{example_exe}.exe"
    example_icon_path = rf"$Icons\{example_exe}.ico,0"
    example_env_value = rf"${{USERPROFILE}}\{identity.name}"
    example_wrapper_name = f"{example_wrapper}.cmd"
    example_wrapper_command = f'"{example_exe_path}" %*'

    lines = [
        "# Generated automatically by `pkg`.",
        "",
        "# UpdateConfig will keep these fields aligned with the package folder name.",
        f"name = {_to_toml_scalar(metadata['name'])}",
        f"version = {_to_toml_scalar(metadata['version'])}",
        f"localVersion = {_to_toml_scalar(metadata['localVersion'])}",
        "# Set only_portable = true when the package stores user config in its folder",
        "# and therefore must only be installed portably.",
        f"only_portable = {_to_toml_scalar(metadata['only_portable'])}",
        "",
        f"# description = {_to_toml_scalar(example_description)}",
        f"# homepage = {_to_toml_scalar(example_homepage)}",
        f"# downloadURL = {_to_toml_scalar(example_download)}",
        "",
        "# Variable expansion reference:",
        "#   $App, $Icons, $Shortcuts -> package directories under <package>/current/",
        "#   ${VAR} -> environment variable expansion and must resolve",
        "#   plain non-package $NAME -> unresolved in regular fields, literal in [[bin]] content",
        "#   $$ -> literal $",
        "",
        "# Examples:",
        "# [[shortcut]]",
        f"# name = {_to_toml_scalar(identity.name)}",
        f"# targetPath = {_to_toml_scalar(example_exe_path)}",
        "# arguments = \"--example\"",
        "# workingDirectory = \"$App\"",
        f"# iconLocation = {_to_toml_scalar(example_icon_path)}",
        f"# description = {_to_toml_scalar(identity.name)}",
        "",
        "# Example environment variable available after installation.",
        "# [[environment]]",
        f"# Name = {_to_toml_scalar(f'{example_env}_HOME')}",
        f"# Value = {_to_toml_scalar(example_env_value)}",
        "",
        "# Example PATH entry.",
        "# [[path]]",
        "# value = \"$App\"",
        "",
        "# Example wrapper script placed in the scope bin directory.",
        "# [[bin]]",
        f"# name = {_to_toml_scalar(example_wrapper_name)}",
        "# content = '''",
        "# @echo off",
        f"# {example_wrapper_command}",
        "# '''",
    ]
    return "\n".join(lines).rstrip() + "\n"


class PackageManager:
    """Orchestrate install and config-update operations for packages."""

    def __init__(
        self,
        scope: Scope = Scope.USER,
        pause: bool = False,
        fix_config: bool = False,
        use_defaults: bool = False,
        force: bool = False,
    ):
        """Create a :class:`PackageManager`.

        Args:
            scope: Installation scope to use for mutations.
            pause: Whether the CLI should pause before exit.
            fix_config: Whether installs may synchronize mismatched metadata
                automatically.
            use_defaults: Whether installs may fall back to runtime defaults when
                TOML loading fails.
            force: Whether installs may replace ``current`` even when it already
                points to a newer version. Ordinary same-version repair reruns do
                not require ``force``.
        """

        self.scope = scope
        self.pause = pause
        self.fix_config = fix_config
        self.use_defaults = use_defaults
        self.force = force

    def _print_banner(self, operation: Action) -> None:
        """Emit the standard CLI banner for one operation.

        Args:
            operation: Action currently being executed.
        """

        log_info("")
        log_info("=" * 60)
        log_info("gurlatsev/pkg: Package Manager")
        log_info(f"Operation: {operation.value}")
        log_info(f"Scope: {self.scope.value}")
        log_info("=" * 60)
        log_info("")

    def _failure(self, message: str, *, exit_code: int, warnings: Optional[List[str]] = None) -> ActionResult:
        """Create a failed action result and report the error.

        Args:
            message: Human-readable error message.
            exit_code: Exit code that should be returned to the caller.
            warnings: Optional list of already-collected warnings.

        Returns:
            An :class:`ActionResult` representing the failure.
        """

        log_error(message)
        return ActionResult(
            ok=False,
            changed=False,
            warnings=warnings or [],
            errors=[message],
            exit_code=exit_code,
        )

    def install(self, package_path: Path) -> ActionResult:
        """Install or reinstall a package and return a truthful action result.

        Same-version installs are intentionally not treated as a no-op. Once the
        selected version is allowed to proceed, the component pipeline reruns so
        broken shortcuts, environment variables, PATH entries, and wrapper files
        can be restored. Depending on *package_path*, reinstall may also refresh
        the ``current`` junction.

        Args:
            package_path: User-supplied path to a version directory, package
                root, or ``current`` junction.

        Returns:
            An :class:`ActionResult` describing the install outcome.
        """

        self._print_banner(Action.INSTALL)

        try:
            package_path, installing_from_current = self._resolve_install_path(package_path)
        except ValueError as exc:
            return self._failure(str(exc), exit_code=EXIT_USER_ERROR)

        try:
            metadata = PackageMetadata(package_path)
            metadata.set_scope(self.scope)
            raw_config_data, load_warnings = metadata.load_config(use_defaults=self.use_defaults)
        except (ConfigValidationError, RuntimeError, ValueError) as exc:
            return self._failure(f"Failed to load package metadata/config: {exc}", exit_code=EXIT_USER_ERROR)
        except OSError as exc:
            return self._failure(f"Failed to load package metadata/config: {exc}", exit_code=EXIT_MUTATION_ERROR)

        warnings = list(load_warnings)
        for warning in load_warnings:
            log_warning(warning)

        config_sync_changed = False
        inconsistencies = check_metadata_consistency(metadata.identity, raw_config_data)
        if inconsistencies:
            if not self.fix_config:
                log_error("Configuration inconsistencies detected:")
                for message in inconsistencies:
                    log_error(f"  - {message}")
                log_info("Aborting installation to avoid mutating configs as a side effect.")
                log_info("To fix the config, run one of:")
                log_info(f"  - pkg --action {Action.UPDATE_CONFIG.value} {metadata.version_path}")
                log_info("  - re-run this install with --fix-config")
                return ActionResult(
                    ok=False,
                    changed=False,
                    warnings=warnings,
                    errors=inconsistencies,
                    exit_code=EXIT_USER_ERROR,
                )

            log_warning("Configuration inconsistencies detected:")
            for message in inconsistencies:
                log_warning(f"  - {message}")
            log_info("--fix-config enabled: syncing configuration metadata to match directory structure...")
            try:
                update_result = metadata.update_config()
            except (ConfigValidationError, RuntimeError, ValueError) as exc:
                return self._failure(f"Failed to update configuration: {exc}", exit_code=EXIT_USER_ERROR, warnings=warnings)
            except OSError as exc:
                return self._failure(f"Failed to update configuration: {exc}", exit_code=EXIT_MUTATION_ERROR, warnings=warnings)
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
            log_info("Configuration updated successfully.")
            try:
                raw_config_data, reload_warnings = metadata.load_config(use_defaults=self.use_defaults)
            except (ConfigValidationError, RuntimeError, ValueError) as exc:
                return self._failure(
                    f"Failed to reload configuration after update: {exc}",
                    exit_code=EXIT_USER_ERROR,
                    warnings=warnings,
                )
            except OSError as exc:
                return self._failure(
                    f"Failed to reload configuration after update: {exc}",
                    exit_code=EXIT_MUTATION_ERROR,
                    warnings=warnings,
                )
            warnings.extend(reload_warnings)
            for warning in reload_warnings:
                log_warning(warning)
            log_info("")

        effective_only_portable = metadata.identity.only_portable_by_name or metadata.only_portable
        log_info(f"Package: {metadata.name}")
        log_info(f"Version: {metadata.version_string}")
        log_info(f"Path: {metadata.version_path}")
        log_info(f"only_portable: {effective_only_portable}")
        log_info("")

        if effective_only_portable and self.scope == Scope.MACHINE:
            return self._failure(
                "only_portable packages cannot be installed system-wide. Please use User scope.",
                exit_code=EXIT_USER_ERROR,
                warnings=warnings,
            )

        if self.scope == Scope.MACHINE and not is_current_user_admin():
            return self._failure(
                "Machine scope requires administrator privileges. Please run as administrator.",
                exit_code=EXIT_USER_ERROR,
                warnings=warnings,
            )

        junction_changed = False
        if installing_from_current:
            log_info("Installing from resolved 'current' target (skipping junction management)")
        else:
            log_info("Managing 'current' junction...")
            try:
                junction_changed = JunctionManager.update_current_junction_if_needed(metadata, force=self.force)
            except ValueError as exc:
                return self._failure(str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings)
            except Exception as exc:
                return self._failure(str(exc), exit_code=EXIT_MUTATION_ERROR, warnings=warnings)

            if not junction_changed and not metadata.is_current:
                log_info("Skipping component installation (newer version already installed)")
                return ActionResult(
                    ok=True,
                    changed=config_sync_changed,
                    warnings=warnings,
                    exit_code=EXIT_SUCCESS,
                )

        log_info("")
        log_info("Installing components...")
        component_result = self._install_components(metadata)
        warnings.extend(component_result.warnings)

        if not component_result.ok:
            log_error("One or more install steps failed:")
            for error in component_result.errors:
                log_error(f"  - {error}")
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
        """Resolve an install input into a concrete version path.

        Args:
            package_path: User-supplied path to resolve.

        Returns:
            Tuple ``(version_path, installing_from_current)``.
        """

        resolved = resolve_input_path(package_path)
        return resolved.version_path, resolved.installing_from_current

    def _install_components(self, metadata: PackageMetadata) -> StepResult:
        """Run the ordered component-install pipeline for one package.

        Args:
            metadata: Package metadata describing the package being installed.

        Returns:
            Aggregated :class:`StepResult` for all component steps.
        """

        if metadata.scope_paths is None:
            metadata.scope_paths = compute_scope_paths(metadata.scope)

        results: List[StepResult] = []
        for step in INSTALL_STEPS:
            step_result = step(metadata)
            results.append(step_result)

        if not results:
            return StepResult(ok=True, changed=False)

        return combine_step_results(*results)

    def update_config(self, package_path: Path) -> ActionResult:
        """Synchronize ``pkg.toml`` metadata for one package.

        Args:
            package_path: User-supplied path to a version directory, package
                root, or ``current`` junction.

        Returns:
            An :class:`ActionResult` describing the metadata-update outcome.
        """

        self._print_banner(Action.UPDATE_CONFIG)

        try:
            resolved_path, _ = self._resolve_install_path(package_path)
        except ValueError as exc:
            return self._failure(str(exc), exit_code=EXIT_USER_ERROR)

        try:
            metadata = PackageMetadata(resolved_path)
            step_result = metadata.update_config()
        except (ConfigValidationError, RuntimeError, ValueError) as exc:
            return self._failure(f"Failed to update configuration: {exc}", exit_code=EXIT_USER_ERROR)
        except OSError as exc:
            return self._failure(f"Failed to update configuration: {exc}", exit_code=EXIT_MUTATION_ERROR)

        return ActionResult(
            ok=step_result.ok,
            changed=step_result.changed,
            warnings=step_result.warnings,
            errors=step_result.errors,
            exit_code=EXIT_SUCCESS if step_result.ok else EXIT_MUTATION_ERROR,
        )


class _ExtendedHelpAction(argparse.Action):
    """Argparse action that prints standard help plus extended documentation."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        """Create the extended-help argparse action.

        Args:
            option_strings: CLI flags that trigger the action.
            dest: Argparse destination name.
            default: Default argparse value.
            help: Help text shown in ``--help`` output.
        """

        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        """Print standard and extended help, then exit.

        Args:
            parser: Active argument parser.
            namespace: Parsed namespace (unused).
            values: Parsed values for the option (unused).
            option_string: Exact CLI option that triggered the action.
        """

        _ = namespace
        _ = values
        _ = option_string
        parser.print_help()
        log_info("")
        log_info(EXTENDED_HELP.strip())
        log_info("")
        parser.exit()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for ``pkg``.

    Args:
        argv: Optional argument list excluding the program name. When omitted,
            :data:`sys.argv[1:]` is used by :mod:`argparse`.

    Returns:
        Process exit code.
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

    # ``pkg.cmd`` forwards ``--python`` to ``pkg.py`` as part of the bootstrap
    # interpreter-selection contract. Keep accepting it here even though normal
    # ``pkg --help`` output hides it; later cleanup should not treat it as dead
    # compatibility debris.
    parser.add_argument(
        "--python",
        default=None,
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in Scope],
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
        help="Force install: allow replacing current even if a newer version is already active",
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
    result: ActionResult

    try:
        manager = PackageManager(
            scope=scope,
            pause=args.pause,
            fix_config=args.fix_config,
            use_defaults=args.use_defaults,
            force=args.force,
        )
        package_path = Path(args.path).expanduser()

        if action == Action.INSTALL:
            result = manager.install(package_path)
        elif action == Action.UPDATE_CONFIG:
            result = manager.update_config(package_path)
        else:
            message = f"Unknown action: {action}"
            log_error(message)
            result = ActionResult(ok=False, errors=[message], exit_code=EXIT_USER_ERROR)
    except Exception as exc:
        message = f"Unexpected internal error: {exc}"
        log_error(message)
        result = ActionResult(ok=False, errors=[message], exit_code=EXIT_INTERNAL_ERROR)

    if args.pause:
        log_info("")
        log_info("Press any key to continue...")
        wait_for_keypress()

    log_info("")
    log_info("-" * 60)
    if result.ok and result.changed:
        log_info(f"{action.value} completed successfully.")
    elif result.ok:
        log_info(f"{action.value} completed successfully (no changes needed).")
    else:
        log_info(f"{action.value} failed.")
    log_info("-" * 60)
    return result.exit_code


#------------------------------------------
# Section: Script entry point
#------------------------------------------
if __name__ == "__main__":
    raise SystemExit(main())
