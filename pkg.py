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
import importlib
import json
import os
import re
import shutil
import tempfile
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
    package configuration, such as missing required keys or invalid value
    shapes.
    """


class DependencyError(RuntimeError):
    """Raised when an optional dependency is required but unavailable.

    ``pkg`` deliberately delays importing TOML and Windows integration backends
    until they are needed. This exception makes those failures explicit and easy
    to classify as user-actionable dependency problems.
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


@dataclass(frozen=True)
class TomlReader:
    """Descriptor for a read-only TOML backend.

    Attributes:
        name: Human-readable backend name, such as ``tomllib`` or ``tomlkit``.
        module: Imported Python module that implements the reader.
    """

    name: str
    module: Any


@dataclass(frozen=True)
class RoundTripBackend:
    """Descriptor for a round-trip TOML backend.

    Attributes:
        name: Human-readable backend name.
        module: Imported module that can preserve formatting/comments when
            parsing and writing TOML documents.
    """

    name: str
    module: Any


@dataclass
class TextConfigDocument:
    """Fallback representation for metadata-only TOML editing.

    When ``tomlkit`` is unavailable, ``pkg`` falls back to a narrowly scoped
    text editor that only updates the metadata fields owned by the tool. This
    lightweight wrapper keeps the interface compatible with ``tomlkit``'s
    ``as_string()`` API.

    Attributes:
        text: Raw TOML text being edited.
    """

    text: str

    def as_string(self) -> str:
        """Return the document text.

        Returns:
            The serialized TOML text stored in :attr:`text`.
        """

        return self.text


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

    ``GENERAL`` expands package variables, ``${VAR}``, and plain ``$VAR``
    environment references. ``SCRIPT`` expands package variables and braced
    references, but preserves plain ``$VAR`` tokens so script languages such as
    PowerShell keep their native variable syntax.
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


class Reporter:
    """Minimal console reporter used by the orchestration layer.

    The reporter intentionally keeps formatting simple so unit tests and humans
    can both understand the output without additional adapters.
    """

    def info(self, msg: str) -> None:
        """Emit an informational message.

        Args:
            msg: Text to print verbatim.
        """

        print(msg)

    def warn(self, msg: str) -> None:
        """Emit a warning message.

        Args:
            msg: Warning text. The ``WARNING:`` prefix is added automatically if
                the caller did not provide it.
        """

        if msg.startswith("WARNING:"):
            print(msg)
        else:
            print(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        """Emit an error message.

        Args:
            msg: Error text. The ``ERROR:`` prefix is added automatically if the
                caller did not provide it.
        """

        if msg.startswith("ERROR:"):
            print(msg)
        else:
            print(f"ERROR: {msg}")


class VariableExpander:
    """Compatibility wrapper around :func:`expand_text`.

    Existing code and tests refer to the older ``VariableExpander`` helper. The
    implementation stays as a thin compatibility layer while the newer API uses
    :func:`expand_text` directly.
    """

    @staticmethod
    def expand_variables(
        text: str,
        metadata: Any,
        mode: ExpansionMode = ExpansionMode.GENERAL,
    ) -> ExpansionResult:
        """Expand one string using a :class:`PackageMetadata` object.

        Args:
            text: Text that may contain package or environment variables.
            metadata: Metadata object whose :attr:`identity` supplies package
                paths.
            mode: Expansion ruleset to apply.

        Returns:
            The expanded value and any unresolved variable tokens.
        """

        return expand_text(text, metadata.identity, mode)

    @staticmethod
    def expand_dict(
        data: Dict[str, str],
        metadata: Any,
        mode: ExpansionMode = ExpansionMode.GENERAL,
    ) -> Dict[str, ExpansionResult]:
        """Expand every value in a dictionary.

        Args:
            data: Mapping of field names to raw text values.
            metadata: Metadata object whose :attr:`identity` supplies package
                paths.
            mode: Expansion ruleset to apply to every value.

        Returns:
            A new mapping whose values are :class:`ExpansionResult` objects.
        """

        return {key: expand_text(value, metadata.identity, mode) for key, value in data.items()}


def try_import(module_name: str) -> Optional[Any]:
    """Import a module only when it is available.

    Args:
        module_name: Fully qualified module name to import.

    Returns:
        The imported module object, or ``None`` when the import fails with
        :class:`ImportError`.
    """

    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


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


def load_toml_reader() -> TomlReader:
    """Select a read-only TOML backend lazily.

    Returns:
        A :class:`TomlReader` describing the chosen backend.

    Raises:
        DependencyError: If no supported TOML reader backend is available.
    """

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
    """Select a round-trip TOML backend lazily.

    Args:
        require: Compatibility parameter retained for older callers. The current
            implementation always returns ``None`` instead of raising when no
            round-trip backend is available.

    Returns:
        A :class:`RoundTripBackend` when ``tomlkit`` is installed; otherwise
        ``None``.
    """

    _ = require
    tomlkit_module = try_import("tomlkit")
    if tomlkit_module is not None:
        return RoundTripBackend("tomlkit", tomlkit_module)
    return None


def read_toml_file(path: Path) -> Dict[str, Any]:
    """Read a TOML file into plain Python data.

    Args:
        path: TOML file to load.

    Returns:
        A dictionary-like tree of plain Python values.

    Raises:
        DependencyError: If no TOML reader backend is available.
        OSError: If the file cannot be read.
        Exception: Backend-specific parse errors.
    """

    reader = load_toml_reader()
    if reader.name == "tomllib":
        with open(path, "rb") as file_handle:
            return reader.module.load(file_handle)
    if reader.name == "tomlkit":
        return reader.module.parse(path.read_text(encoding="utf-8")).unwrap()
    with open(path, "r", encoding="utf-8") as file_handle:
        return reader.module.load(file_handle)


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
    - Plain ``$VAR`` expands only in :class:`ExpansionMode.GENERAL`.
    - Plain ``$VAR`` stays literal in :class:`ExpansionMode.SCRIPT`, except for
      package variables.
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


def get_win32com_client() -> Optional[Any]:
    """Return ``win32com.client`` when it is available.

    Returns:
        The imported ``win32com.client`` module, or ``None`` when ``pywin32``
        is not installed.
    """

    return try_import("win32com.client")


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


def get_shortcut_backend() -> str:
    """Choose the runtime backend used for ``.lnk`` creation.

    Returns:
        ``"pywin32"`` when ``pywin32`` is available; otherwise ``"powershell"``.
    """

    return "pywin32" if get_win32com_client() is not None else "powershell"


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


def create_shortcut_with_pywin32(prepared: PreparedShortcut) -> None:
    """Create one ``.lnk`` file through ``pywin32`` automation.

    Args:
        prepared: Fully expanded shortcut data to write.

    Raises:
        RuntimeError: If ``pywin32`` is unavailable or COM automation fails.
    """

    win32_client = get_win32com_client()
    if win32_client is None:
        raise RuntimeError("pywin32 is not installed")

    shell = win32_client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(prepared.shortcut_path))
    shortcut.TargetPath = prepared.target_path
    shortcut.Arguments = prepared.arguments
    shortcut.WorkingDirectory = prepared.working_directory
    if prepared.icon_location:
        shortcut.IconLocation = prepared.icon_location
    shortcut.Description = prepared.description
    shortcut.Save()


def create_shortcut_with_powershell(prepared: PreparedShortcut) -> None:
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


def create_shortcut(prepared: PreparedShortcut, backend: Optional[str] = None) -> str:
    """Create one shortcut using the selected backend.

    Args:
        prepared: Fully expanded shortcut data to write.
        backend: Explicit backend override. When omitted, the preferred backend
            is selected automatically.

    Returns:
        The backend name that performed the write.

    Raises:
        RuntimeError: If the selected backend cannot create the shortcut.
    """

    selected_backend = backend or get_shortcut_backend()
    if selected_backend == "pywin32":
        create_shortcut_with_pywin32(prepared)
        return selected_backend
    create_shortcut_with_powershell(prepared)
    return selected_backend


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

InstallStep = Callable[["PackageMetadata", Reporter], StepResult]


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
                print(f"JUNCTION: stale current target detected: {current_target}")
            else:
                if current_target.parent != metadata.pkg_path:
                    raise ValueError(
                        f"{current_path} is a junction but its target {current_target} "
                        f"is not under {metadata.pkg_path}. Aborting."
                    )

                current_version = current_target.name
                print(f"'current' junction version: {current_version}")
                comparison = JunctionManager.compare_versions(metadata.version_string, current_version)
                # Same-version reinstalls are a supported refresh path. Only
                # keep the existing junction untouched when it points to a
                # newer version and --force was not requested.
                if not force and comparison < 0:
                    print(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
                    return False
                if force:
                    print(f"JUNCTION: --force: updating current to {metadata.version_string}")
        
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

        print(f"JUNCTION: created: {current_path.name} -> {desired_target}")
        return True


class ShortcutInstaller:
    """Expand shortcut config and orchestrate Start Menu shortcut creation."""

    @staticmethod
    def _prepare_shortcut(shortcut_info: Dict[str, str], metadata: "PackageMetadata") -> PreparedShortcut:
        """Normalize and expand shortcut data before backend-specific writing.

        Args:
            shortcut_info: Raw shortcut dictionary from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            A fully expanded :class:`PreparedShortcut` object.
        """

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
        metadata: "PackageMetadata",
    ) -> Tuple[bool, Optional[str]]:
        """Create a shortcut using the ``pywin32`` wrapper.

        Args:
            shortcut_info: Raw shortcut dictionary from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            ``(True, None)`` on success; otherwise ``(False, error_message)``.
        """

        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_info, metadata)
            create_shortcut_with_pywin32(prepared)
            print(f"SHORTCUT: created: {prepared.shortcut_path.name}")
            return True, None
        except Exception as exc:
            name = shortcut_info.get("name", "unknown")
            print(f"SHORTCUT error creating {name}: {exc}")
            return False, f"Failed to create shortcut '{name}': {exc}"

    @staticmethod
    def _create_shortcut_with_powershell(
        shortcut_info: Dict[str, str],
        metadata: "PackageMetadata",
    ) -> Tuple[bool, Optional[str]]:
        """Create a shortcut using the PowerShell wrapper.

        Args:
            shortcut_info: Raw shortcut dictionary from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            ``(True, None)`` on success; otherwise ``(False, error_message)``.
        """

        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_info, metadata)
            create_shortcut_with_powershell(prepared)
            print(f"SHORTCUT: created (via PowerShell): {prepared.shortcut_path.name}")
            return True, None
        except Exception as exc:
            name = shortcut_info.get("name", "unknown")
            print(f"SHORTCUT error creating {name}: {exc}")
            return False, f"Failed to create shortcut '{name}': {exc}"

    @staticmethod
    def create_shortcut(
        shortcut_info: Dict[str, str],
        metadata: "PackageMetadata",
        backend: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Create a single ``.lnk`` shortcut.

        Args:
            shortcut_info: Raw shortcut dictionary from the runtime config.
            metadata: Package metadata for the package being installed.
            backend: Explicit backend override. When omitted, the preferred
                runtime backend is selected automatically.

        Returns:
            ``(True, None)`` on success; otherwise ``(False, error_message)``.
        """

        try:
            prepared = ShortcutInstaller._prepare_shortcut(shortcut_info, metadata)
            selected_backend = create_shortcut(prepared, backend=backend)
            if selected_backend == "pywin32":
                print(f"SHORTCUT: created: {prepared.shortcut_path.name}")
            else:
                print(f"SHORTCUT: created (via PowerShell): {prepared.shortcut_path.name}")
            return True, None
        except Exception as exc:
            name = shortcut_info.get("name", "unknown")
            print(f"SHORTCUT error creating {name}: {exc}")
            return False, f"Failed to create shortcut '{name}': {exc}"

    @staticmethod
    def install_shortcuts(metadata: "PackageMetadata", reporter: Optional[Reporter] = None) -> StepResult:
        """Install every shortcut declared by a package.

        Args:
            metadata: Package metadata whose ``shortcut`` field contains the
                shortcut declarations.
            reporter: Optional reporter used for user-visible warnings and
                errors.

        Returns:
            A :class:`StepResult` summarizing the shortcut step.
        """

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
            print(f"Warning: failed to broadcast environment change notification: {exc}")

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
            print(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
            EnvironmentVariableManager.broadcast_environment_change()
            return True
        except PermissionError:
            print(f"ERROR: Insufficient permissions to set {scope.value} environment variable: {name}")
            return False
        except Exception as exc:
            print(f"ENVIRONMENT error setting {name}: {exc}")
            return False

    @staticmethod
    def install_environment_variables(metadata: "PackageMetadata", reporter: Optional[Reporter] = None) -> StepResult:
        """Install every environment variable declared by a package.

        Args:
            metadata: Package metadata whose ``environment`` field contains the
                variable declarations.
            reporter: Optional reporter used for user-visible warnings and
                errors.

        Returns:
            A :class:`StepResult` summarizing the environment-variable step.
        """

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
            print(f"PATH error reading {scope.value} PATH: {exc}")

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
            print(f"ERROR: Insufficient permissions to set {scope.value} PATH")
            return False
        except Exception as exc:
            print(f"PATH error setting {scope.value} PATH: {exc}")
            return False

    @staticmethod
    def add_to_path(new_entries: List[str], metadata: "PackageMetadata", reporter: Optional[Reporter] = None) -> StepResult:
        """Append directories to PATH while avoiding duplicates.

        Args:
            new_entries: Candidate directories to add.
            metadata: Package metadata used for variable expansion and scope
                selection.
            reporter: Optional reporter used for user-visible warnings and
                errors.

        Returns:
            A :class:`StepResult` summarizing the PATH update.
        """

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
        existing_keys = {PATHManager._path_key(item) for item in current_path if item}
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
        """Return the system drive root, defaulting to ``C:\\``.

        Returns:
            A drive-root string ending with a trailing backslash.
        """

        return system_drive_root()

    @staticmethod
    def ensure_bin_in_path(metadata: "PackageMetadata", reporter: Optional[Reporter] = None) -> StepResult:
        """Ensure the per-scope ``bin`` directory exists and is on PATH.

        Args:
            metadata: Package metadata that determines the target scope.
            reporter: Optional reporter used for user-visible warnings and
                errors.

        Returns:
            A :class:`StepResult` summarizing the bin-directory and PATH work.
        """

        reporter = reporter or Reporter()
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
            path_result = PATHManager.add_to_path([bin_dir_str], metadata, reporter=reporter)
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
    def create_wrapper(wrapper_info: Dict[str, str], metadata: "PackageMetadata") -> Tuple[bool, Optional[str]]:
        """Create one wrapper file.

        Args:
            wrapper_info: Raw wrapper dictionary from the runtime config.
            metadata: Package metadata for the package being installed.

        Returns:
            ``(True, None)`` when the wrapper changed, ``(True, "unchanged")``
            when it was already up to date, or ``(False, error_message)`` on
            failure.
        """

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

            extension = wrapper_path.suffix.lower()
            if extension in (".cmd", ".bat"):
                try:
                    desired_bytes = expanded_content.encode("ascii")
                except UnicodeEncodeError:
                    print(
                        f"Warning: non-ASCII content in {extension} wrapper; writing UTF-8 with BOM: {wrapper_path.name}"
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
        except Exception as exc:
            name = wrapper_info.get("name", "unknown")
            print(f"BIN error creating {name}: {exc}")
            return False, f"Failed to create wrapper '{name}': {exc}"

    @staticmethod
    def install_wrappers(metadata: "PackageMetadata", reporter: Optional[Reporter] = None) -> StepResult:
        """Install every wrapper declared by a package.

        Args:
            metadata: Package metadata whose ``bin`` field contains wrapper
                declarations.
            reporter: Optional reporter used for user-visible warnings and
                errors.

        Returns:
            A :class:`StepResult` summarizing the wrapper-install step.
        """

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


def install_shortcuts_step(metadata: "PackageMetadata", reporter: Reporter) -> StepResult:
    """Run the shortcut-install step for one package.

    Args:
        metadata: Package metadata to install.
        reporter: Reporter used for user-visible logging.

    Returns:
        A :class:`StepResult` describing the shortcut step outcome.
    """

    if not metadata.shortcut:
        return StepResult(ok=True, changed=False)
    reporter.info("")
    reporter.info("Creating shortcuts...")
    return ShortcutInstaller.install_shortcuts(metadata, reporter=reporter)


def install_environment_variables_step(metadata: "PackageMetadata", reporter: Reporter) -> StepResult:
    """Run the environment-variable step for one package.

    Args:
        metadata: Package metadata to install.
        reporter: Reporter used for user-visible logging.

    Returns:
        A :class:`StepResult` describing the environment-variable step outcome.
    """

    if not metadata.environment:
        return StepResult(ok=True, changed=False)
    reporter.info("")
    reporter.info("Setting environment variables...")
    return EnvironmentVariableManager.install_environment_variables(metadata, reporter=reporter)


def ensure_bin_in_path_step(metadata: "PackageMetadata", reporter: Reporter) -> StepResult:
    """Run the scope ``bin`` directory/PATH bootstrap step.

    Args:
        metadata: Package metadata to install.
        reporter: Reporter used for user-visible logging.

    Returns:
        A :class:`StepResult` describing the bin-directory/PATH step outcome.
    """

    reporter.info("")
    reporter.info("Managing PATH...")
    return PATHManager.ensure_bin_in_path(metadata, reporter=reporter)


def install_extra_path_entries_step(metadata: "PackageMetadata", reporter: Reporter) -> StepResult:
    """Run the package-specific extra PATH entry step.

    Args:
        metadata: Package metadata to install.
        reporter: Reporter used for user-visible logging.

    Returns:
        A :class:`StepResult` describing the extra PATH entry step outcome.
    """

    if not metadata.path:
        return StepResult(ok=True, changed=False)
    return PATHManager.add_to_path(metadata.path, metadata, reporter=reporter)


def install_wrappers_step(metadata: "PackageMetadata", reporter: Reporter) -> StepResult:
    """Run the wrapper-install step for one package.

    Args:
        metadata: Package metadata to install.
        reporter: Reporter used for user-visible logging.

    Returns:
        A :class:`StepResult` describing the wrapper step outcome.
    """

    if not metadata.bin:
        return StepResult(ok=True, changed=False)
    reporter.info("")
    reporter.info("Creating executable wrappers...")
    return BinFileCreator.install_wrappers(metadata, reporter=reporter)


INSTALL_STEPS: Tuple[InstallStep, ...] = (
    install_shortcuts_step,
    install_environment_variables_step,
    ensure_bin_in_path_step,
    install_extra_path_entries_step,
    install_wrappers_step,
)


class WindowsPlatform:
    """Small Windows boundary used by package-management orchestration.

    The package-management layer keeps a concrete platform object for the few
    operations that are still convenient to patch in tests or centralize at one
    boundary.
    """

    def resolve_input_path(self, raw_path: Path) -> ResolvedInput:
        """Delegate path resolution to :func:`resolve_input_path`.

        Args:
            raw_path: User-supplied package path.

        Returns:
            A :class:`ResolvedInput` describing the chosen version directory.
        """

        return resolve_input_path(raw_path)

    def is_admin(self) -> bool:
        """Return whether the current process has Administrator privileges.

        Returns:
            ``True`` when the current process is elevated; otherwise ``False``.
        """

        return is_current_user_admin()

    def pause_if_requested(self, pause: bool) -> None:
        """Pause for a keypress when requested by the CLI.

        Args:
            pause: Whether the pause should happen.
        """

        if not pause:
            return
        print()
        print("Press any key to continue...")
        wait_for_keypress()

    def update_current_junction_if_needed(self, metadata: "PackageMetadata", *, force: bool = False) -> bool:
        """Run package-level junction-update policy.

        Args:
            metadata: Package metadata describing the version being installed.
            force: Whether replacing ``current`` should be allowed when it
                already points to a newer version.

        Returns:
            ``True`` when ``current`` was recreated or repointed.
        """

        return JunctionManager.update_current_junction_if_needed(metadata, force=force)


DEFAULT_PLATFORM = WindowsPlatform()


def pause_if_requested(pause: bool) -> None:
    """Pause for a keypress using the default platform facade.

    Args:
        pause: Whether the pause should happen.
    """

    DEFAULT_PLATFORM.pause_if_requested(pause)


EXTENDED_HELP = r"""
Extended help
-------------

Quick start
~~~~~~~~~~~

Notes:
  - ``pkg --help`` and ``pkg --version`` do not install dependencies or write files.
  - ``Install`` does not auto-create ``pkg.toml``.
  - ``UpdateConfig`` creates a documented starter template with commented examples when ``pkg.toml`` is missing.
  - ``UpdateConfig`` preserves comments, unknown keys, and existing TOML structure when updating an existing file.
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

Config keys and examples
~~~~~~~~~~~~~~~~~~~~~~~~

1) Shortcuts (``shortcut`` list)
   Supported keys:

   - ``name`` (required): file name without extension or with ``.lnk``
   - ``targetPath`` (required; alias: ``path``): executable path
   - ``arguments`` (optional)
   - ``workingDirectory`` (optional)
   - ``iconLocation`` (optional): e.g. ``C:\path\icon.ico,0``
   - ``description`` (optional)

2) Environment variables (``environment`` list)
   Canonical keys are ``Name`` and ``Value``.

3) PATH additions (``path`` list)
   Use repeated ``[[path]]`` tables or a list-like internal representation.

4) Bin wrappers (``bin`` list)
   Each entry has ``name`` and ``content``. Wrapper content uses the script
   expansion mode so PowerShell variables such as ``$PSScriptRoot`` remain
   literal unless they are package variables.

Variable expansion
~~~~~~~~~~~~~~~~~~

- ``$App``, ``$Icons``, and ``$Shortcuts`` expand everywhere.
- ``${VAR}`` expands everywhere and must resolve.
- plain ``$VAR`` expands only in general config fields.
- plain ``$VAR`` remains literal inside wrapper content.
"""

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
    "portable": "only_portable",
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


def normalize_runtime_config(raw: Any, identity: PackageIdentity) -> PackageConfig:
    """Normalize raw config data into the runtime model.

    Args:
        raw: Parsed TOML data or ``None``.
        identity: Directory-derived package identity used to supply defaults.

    Returns:
        A fully normalized :class:`PackageConfig` object.

    Raises:
        ConfigValidationError: If *raw* is not a mapping or contains invalid
            structures.
    """

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


def package_config_to_dict(config: PackageConfig, identity: PackageIdentity) -> Dict[str, Any]:
    """Convert a runtime config back to a plain dictionary.

    Args:
        config: Normalized runtime config.
        identity: Package identity that supplies owned metadata fields.

    Returns:
        A plain-Python dictionary equivalent of *config*.
    """

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

    data = PackageMetadata._canonicalize_config_dict(dict(raw_config))

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
    """Read and validate ``pkg.toml`` for one package version.

    Args:
        identity: Package identity whose ``pkg.toml`` should be loaded.
        use_defaults: Whether to fall back to defaults when parsing or
            validation fails.

    Returns:
        A tuple ``(runtime_config, raw_dict, warnings)`` where ``raw_dict`` is
        the canonicalized file-authored config when ``pkg.toml`` exists, or the
        generated defaults dictionary when no config is available.

    Raises:
        DependencyError: If a TOML reader is required but unavailable and
            *use_defaults* is ``False``.
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
            raw_config = PackageMetadata._canonicalize_config_dict(dict(loaded))
            return config, raw_config, warnings
        except DependencyError:
            if not use_defaults:
                raise
            warnings.append(
                "No TOML reader is available for pkg.toml parsing. Proceeding with defaults because --use-defaults was provided."
            )
        except ConfigValidationError:
            raise
        except Exception as exc:
            if not use_defaults:
                raise RuntimeError(f"Error loading TOML config from {toml_path}: {exc}") from exc
            warnings.append(f"Error loading TOML config from {toml_path}: {exc}")
            warnings.append("Proceeding with defaults because --use-defaults was provided.")
    config = normalize_runtime_config({}, identity)
    validate_runtime_config(config)
    raw_config = package_config_to_dict(config, identity)
    warnings.append(f"No pkg.toml found at {toml_path}; using defaults without creating a file.")
    return config, raw_config, warnings


class PackageMetadata:
    """Compatibility facade for package identity, scope, and config data."""

    def __init__(self, version_path: Path, platform: Optional[WindowsPlatform] = None):
        """Create package metadata for a concrete version directory.

        Args:
            version_path: Path pointing at a version directory, the package root,
                or the ``current`` junction.
            platform: Optional Windows platform facade used for path resolution
                and scope-path calculation.
        """

        self.platform = platform or DEFAULT_PLATFORM
        resolved = self.platform.resolve_input_path(Path(version_path))
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

    def check_metadata_consistency(self, config_data: Dict[str, Any]) -> List[str]:
        """Check directory/config metadata consistency for this package.

        Args:
            config_data: Raw config dictionary to compare against the directory
                identity.

        Returns:
            A list of mismatch descriptions.
        """

        return check_metadata_consistency(self.identity, config_data)

    @staticmethod
    def _canonicalize_dict_keys(data: Dict[str, Any], keymap: Dict[str, str], *, context: str) -> Dict[str, Any]:
        """Canonicalize mapping keys using a case-insensitive alias map.

        Args:
            data: Source dictionary to normalize.
            keymap: Mapping from lower-cased aliases to canonical key names.
            context: Human-readable context string used in error messages.

        Returns:
            A new dictionary whose known keys use the canonical spelling.

        Raises:
            ConfigValidationError: If two source keys collapse to the same
                canonical key.
        """

        out: Dict[str, Any] = {}
        seen_from: Dict[str, str] = {}

        for key, value in data.items():
            key_text = str(key)
            canonical = keymap.get(key_text.lower(), key_text)

            if canonical in out and seen_from.get(canonical) != key_text:
                raise ConfigValidationError(
                    f"Duplicate keys differing only by case/alias in {context}: "
                    f"'{seen_from.get(canonical)}' and '{key_text}' both map to '{canonical}'."
                )

            out[canonical] = value
            seen_from[canonical] = key_text

        return out

    @staticmethod
    def _normalize_bin_content(value: Any) -> str:
        """Normalize wrapper content so escaped newlines become real newlines.

        Args:
            value: Raw wrapper content value from the config.

        Returns:
            Wrapper content with ``\n`` and ``\r\n`` escapes expanded when the
            source contains no actual newline characters.
        """

        text = str(value or "")
        if "\n" in text:
            return text
        return text.replace("\\r\\n", "\n").replace("\\n", "\n")

    @staticmethod
    def _canonicalize_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        """Canonicalize supported config keys and normalize field shapes.

        Args:
            data: Raw configuration dictionary.

        Returns:
            A normalized dictionary whose keys use canonical spellings and whose
            list-like fields use predictable shapes.

        Raises:
            ConfigValidationError: If the config structure is invalid.
        """

        if not isinstance(data, dict):
            raise ConfigValidationError(f"Configuration must be a dict, got: {type(data).__name__}")

        out = PackageMetadata._canonicalize_dict_keys(
            data,
            TOP_LEVEL_CONFIG_KEY_ALIASES,
            context="config",
        )

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

        for field_name in CONFIG_LIST_FIELDS:
            if out.get(field_name, None) is None:
                out[field_name] = []

        if isinstance(out.get("path", None), str):
            out["path"] = [out["path"]]
        elif not isinstance(out.get("path", []), list):
            raise ConfigValidationError(
                f"'path' must be a list of strings or [[path]] tables, got: {type(out['path']).__name__}"
            )

        def canonicalize_block(block_name: str, keymap: Dict[str, str]) -> List[Dict[str, Any]]:
            """Canonicalize one list-of-dicts block.

            Args:
                block_name: Top-level config key being normalized.
                keymap: Alias map for entries within the block.

            Returns:
                A list of canonicalized dictionaries.

            Raises:
                ConfigValidationError: If the block is not a list of
                    dictionaries.
            """

            raw = out.get(block_name, [])
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise ConfigValidationError(f"'{block_name}' must be a list, got: {type(raw).__name__}")
            result: List[Dict[str, Any]] = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise ConfigValidationError(
                        f"'{block_name}[{index}]' must be a dict, got: {type(item).__name__}"
                    )
                result.append(PackageMetadata._canonicalize_dict_keys(item, keymap, context=f"{block_name}[{index}]"))
            return result

        out["environment"] = canonicalize_block("environment", ENVIRONMENT_KEY_ALIASES)
        out["bin"] = canonicalize_block("bin", BIN_KEY_ALIASES)
        for wrapper in out["bin"]:
            if "content" in wrapper:
                wrapper["content"] = PackageMetadata._normalize_bin_content(wrapper.get("content", ""))
        out["shortcut"] = canonicalize_block("shortcut", SHORTCUT_KEY_ALIASES)

        normalized_path: List[str] = []
        for index, entry in enumerate(out.get("path", [])):
            if entry is None:
                continue
            if isinstance(entry, str):
                normalized_path.append(entry)
                continue
            if isinstance(entry, dict):
                path_item = PackageMetadata._canonicalize_dict_keys(entry, PATH_ENTRY_KEY_ALIASES, context=f"path[{index}]")
                value = path_item.get("value", None)
                if value is None:
                    raise ConfigValidationError(
                        f"'path[{index}]' table is missing required key: value (present keys: {', '.join(sorted(str(k) for k in path_item.keys()))})"
                    )
                if not isinstance(value, str):
                    raise ConfigValidationError(f"'path[{index}].value' must be a string, got: {type(value).__name__}")
                normalized_path.append(value)
                continue
            raise ConfigValidationError(
                f"'path[{index}]' must be a string or a dict ([[path]] table), got: {type(entry).__name__}"
            )
        out["path"] = normalized_path

        return out

    def load_config(self, *, use_defaults: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        """Load and normalize the package runtime configuration.

        Args:
            use_defaults: Whether defaults may be used when TOML loading fails.

        Returns:
            A tuple ``(raw_config_dict, warnings)``.
        """

        config, raw_data, warnings = read_runtime_config(self.identity, use_defaults=use_defaults)
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
        return raw_data, warnings

    def update_config(self, reporter: Optional[Reporter] = None) -> StepResult:
        """Synchronize owned metadata fields back to ``pkg.toml``.

        Args:
            reporter: Optional reporter used for user-visible progress and
                warnings.

        Returns:
            A :class:`StepResult` describing the update. Missing configs are
            created as documented starter templates, and bare metadata-only
            configs from the recent regression are upgraded in place.
        """

        reporter = reporter or Reporter()
        toml_path = self.version_path / "pkg.toml"
        json_path = self.version_path / "pkg.json"
        warnings: List[str] = []

        if toml_path.exists():
            original_text = toml_path.read_text(encoding="utf-8")
            if is_metadata_only_config_text(original_text):
                rendered = create_starter_config(self.identity)
                if rendered == original_text:
                    reporter.info(f"Configuration already up to date: {toml_path}")
                    result = StepResult(ok=True, changed=False)
                else:
                    write_text_atomic(toml_path, rendered, backup=True)
                    reporter.info(
                        f"Expanded: {toml_path} (upgraded metadata-only config to documented template)"
                    )
                    result = StepResult(ok=True, changed=True)
            else:
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
            except OSError as exc:
                warning = f"Failed to remove legacy JSON config {json_path}: {exc}"
                reporter.warn(warning)
                warnings.append(warning)

        result.warnings.extend(warnings)
        return result

    def set_scope(self, scope: Scope) -> None:
        """Store the install scope and precompute scope-specific paths.

        Args:
            scope: Selected installation scope.
        """

        self.scope = scope
        self.scope_paths = compute_scope_paths(scope)
        self.shortcut_dir = self.scope_paths.shortcut_root


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


def is_metadata_only_config_text(text: str) -> bool:
    """Return whether *text* is a bare metadata-only config.

    The detection is intentionally conservative and is only used to identify
    the recent regression where ``UpdateConfig`` created a nearly empty
    ``pkg.toml`` containing just the directory-owned metadata fields. When the
    file appears to be that minimal auto-generated shape, ``pkg`` upgrades it
    to the richer documented template on the next ``UpdateConfig`` run.

    Args:
        text: Raw ``pkg.toml`` content to inspect.

    Returns:
        ``True`` when *text* contains only owned metadata assignments (with an
        optional ``[[main]]`` wrapper) plus blank lines; otherwise ``False``.
    """

    allowed_keys = {
        alias.lower()
        for aliases in OWNED_METADATA_KEY_ALIASES.values()
        for alias in aliases
    }
    saw_assignment = False
    saw_main = False

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return False
        if "#" in stripped:
            return False
        if stripped == "[[main]]":
            if saw_main:
                return False
            saw_main = True
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*.+$", stripped)
        if match is None:
            return False
        if match.group(1).lower() not in allowed_keys:
            return False
        saw_assignment = True

    return saw_assignment


def load_config_document(path: Path) -> Tuple[Any, str]:
    """Load an existing ``pkg.toml`` for metadata synchronization.

    Args:
        path: Existing TOML file path.

    Returns:
        Tuple ``(document, original_text)`` where *document* is either a
        round-trip TOML document or a :class:`TextConfigDocument` fallback.

    Raises:
        ConfigValidationError: If the existing TOML document cannot be parsed
            safely for updates.
    """

    original_text = path.read_text(encoding="utf-8")
    backend = load_roundtrip_toml_backend(require=False)
    if backend is not None:
        try:
            return backend.module.parse(original_text), original_text
        except Exception as exc:
            raise ConfigValidationError(
                f"pkg.toml is structurally invalid and cannot be updated safely: {exc}. Edit the config manually."
            ) from exc
    return TextConfigDocument(original_text), original_text


def locate_metadata_container(doc: Any) -> Any:
    """Locate the TOML object that owns package metadata fields.

    Args:
        doc: Parsed TOML document or fallback text document.

    Returns:
        The document object or table that should be updated with owned metadata.

    Raises:
        ConfigValidationError: If an existing ``[[main]]`` section is malformed.
    """

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
    """Find the existing spelling or alias for a metadata field.

    Args:
        container: TOML mapping to search.
        canonical_key: Canonical metadata key name.

    Returns:
        The existing key spelling to update, or ``None`` when the field is not
        present.
    """

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
    """Synchronize owned metadata directly in raw TOML text.

    Args:
        doc: Fallback text document to mutate.
        identity: Package identity that supplies the target metadata values.

    Returns:
        ``True`` when the text changed; otherwise ``False``.

    Raises:
        ConfigValidationError: If a malformed ``[[main]]`` section prevents safe
            updating.
    """

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
        alias_pattern = "|".join(re.escape(alias) for alias in aliases)
        pattern = re.compile(
            rf'(?mi)^(?P<indent>\s*)(?P<key>{alias_pattern})\s*=\s*(?P<value>[^\n#]*)(?P<comment>\s*(?:#.*)?)$'
        )
        match = pattern.search(container_text)
        rendered_value = _to_toml_scalar(value)
        if match:
            existing_value = match.group("value").strip()
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
    """Mutate owned metadata fields in an existing TOML document.

    Args:
        doc: Parsed TOML document or fallback text document.
        identity: Package identity that supplies the target metadata values.

    Returns:
        ``True`` when the document changed; otherwise ``False``.
    """

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
    example_description = f""
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
        "# Therefore the package must only be installed portably.",
        f"only_portable = {_to_toml_scalar(metadata['only_portable'])}",
        "",
        "",
        f"# description = {_to_toml_scalar(example_description)}",
        f"# homepage = {_to_toml_scalar(example_homepage)}",
        f"# downloadURL = {_to_toml_scalar(example_download)}",
        "",
        "# Variable expansion reference:",
        "#   $App, $Icons, $Shortcuts -> package directories under <package>/current/",
        "#   ${VAR} -> environment variable expansion and must resolve",
        "#   plain $VAR -> expands in regular fields but stays literal in [[bin]] content",
        "#   $$ -> literal $",
        "",
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
        no_autoupdate_config: bool = False,
        platform: Optional[WindowsPlatform] = None,
    ):
        """Create a :class:`PackageManager`.

        Args:
            scope: Installation scope to use for mutations.
            pause: Whether the CLI should pause before exit.
            fix_config: Whether installs may synchronize mismatched metadata
                automatically.
            use_defaults: Whether installs may fall back to runtime defaults when
                TOML loading fails.
            force: Whether installs may replace ``current`` even when it
                already points to a newer version. Ordinary same-version repair
                reruns do not require ``force``.
            no_autoupdate_config: Deprecated compatibility flag that disables
                automatic metadata repair during install.
            platform: Optional Windows platform facade that performs all
                Windows-specific work.
        """

        self.scope = scope
        self.pause = pause
        self.fix_config = fix_config
        self.use_defaults = use_defaults
        self.force = force
        self.reporter = Reporter()
        self.platform = platform or DEFAULT_PLATFORM

        if no_autoupdate_config:
            self.fix_config = False

    def _print_banner(self, operation: Action) -> None:
        """Emit the standard CLI banner for one operation.

        Args:
            operation: Action currently being executed.
        """

        self.reporter.info("")
        self.reporter.info("=" * 60)
        self.reporter.info("gurlatsev/pkg: Package Manager")
        self.reporter.info(f"Operation: {operation.value}")
        self.reporter.info(f"Scope: {self.scope.value}")
        self.reporter.info("=" * 60)
        self.reporter.info("")

    def _failure(self, message: str, *, exit_code: int, warnings: Optional[List[str]] = None) -> ActionResult:
        """Create a failed action result and report the error.

        Args:
            message: Human-readable error message.
            exit_code: Exit code that should be returned to the caller.
            warnings: Optional list of already-collected warnings.

        Returns:
            An :class:`ActionResult` representing the failure.
        """

        self.reporter.error(message)
        return ActionResult(
            ok=False,
            changed=False,
            warnings=warnings or [],
            errors=[message],
            exit_code=exit_code,
        )

    def install(self, package_path: Path) -> ActionResult:
        """Install or reinstall a package and return a truthful action result.

        Same-version installs are intentionally not treated as a no-op. Once
        the selected version is allowed to proceed, the component pipeline
        reruns so broken shortcuts, environment variables, PATH entries, and
        wrapper files can be restored. Depending on *package_path*, reinstall
        may also refresh the ``current`` junction.

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
            metadata = PackageMetadata(package_path, platform=self.platform)
            metadata.set_scope(self.scope)
            raw_config_data, load_warnings = metadata.load_config(use_defaults=self.use_defaults)
        except DependencyError as exc:
            return self._failure(str(exc), exit_code=EXIT_USER_ERROR)
        except (ConfigValidationError, RuntimeError, ValueError) as exc:
            return self._failure(f"Failed to load package metadata/config: {exc}", exit_code=EXIT_USER_ERROR)
        except OSError as exc:
            return self._failure(f"Failed to load package metadata/config: {exc}", exit_code=EXIT_MUTATION_ERROR)

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
        inconsistencies = metadata.check_metadata_consistency(raw_config_data)
        if inconsistencies:
            if not self.fix_config:
                self.reporter.error("Configuration inconsistencies detected:")
                for message in inconsistencies:
                    self.reporter.error(f"  - {message}")
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
            for message in inconsistencies:
                self.reporter.warn(f"  - {message}")
            self.reporter.info("--fix-config enabled: syncing configuration metadata to match directory structure...")
            try:
                update_result = metadata.update_config(reporter=self.reporter)
            except DependencyError as exc:
                return self._failure(str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings)
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
            self.reporter.info("Configuration updated successfully.")
            self.reporter.info("")

        if self.scope == Scope.MACHINE and not self.platform.is_admin():
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
                junction_changed = self.platform.update_current_junction_if_needed(metadata, force=self.force)
            except ValueError as exc:
                return self._failure(str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings)
            except Exception as exc:
                return self._failure(str(exc), exit_code=EXIT_MUTATION_ERROR, warnings=warnings)

            # Only skip when a newer version remains current. Reinstalling the
            # active version is intentionally allowed to continue so install
            # can repair external state.
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
        """Resolve an install input into a concrete version path.

        Args:
            package_path: User-supplied path to resolve.

        Returns:
            Tuple ``(version_path, installing_from_current)``.
        """

        resolved = self.platform.resolve_input_path(package_path)
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
            step_result = step(metadata, self.reporter)
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
            metadata = PackageMetadata(resolved_path, platform=self.platform)
            step_result = metadata.update_config(reporter=self.reporter)
        except DependencyError as exc:
            return self._failure(str(exc), exit_code=EXIT_USER_ERROR)
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
        print("\n" + EXTENDED_HELP.strip() + "\n")
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
        help="Force install: allow replacing current even if a newer version is already active",
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
    except Exception as exc:
        message = f"Unexpected internal error: {exc}"
        reporter.error(message)
        result = ActionResult(ok=False, errors=[message], exit_code=EXIT_INTERNAL_ERROR)
    finally:
        DEFAULT_PLATFORM.pause_if_requested(args.pause)

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


#------------------------------------------
# Section: Script entry point
#------------------------------------------
if __name__ == "__main__":
    raise SystemExit(main())
