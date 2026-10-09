"""Synchronize and query the official, read-only ``pkgs/`` registry tree.

The registry is a ZIP archive (by default GitHub's archive of the ``stable``
tag) that contains a ``pkgs`` folder. It is fetched with the Python standard
library, so neither Git nor any extra tool is needed, and the unpacked tree is
validated before it becomes visible to callers. It never imports or executes
package-local hooks; payload acquisition remains the responsibility of the
ordinary package installation workflow.

Usage and API
-------------
Call ``sync_registry(...)`` to refresh a cache, ``registry_status(...)`` to
inspect its active revision, and ``search_registry(...)`` or
``resolve_selector(...)`` for deterministic offline catalog operations.

Implementation Approach
-----------------------
The archive is downloaded into a disposable directory, identified by its
commit ID (or content hash), and only its ``pkgs`` folder is extracted with
path-safety checks. The tree is validated with the existing manifest
normalizer, then copied into a revision-addressed cache and published by an
atomic state file update. Failed work is discarded while the previous active
revision remains usable.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .configuration import check_metadata_consistency, read_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    PackageIdentity,
    VERSION_DIR_NAME_RE,
    log_info,
    write_text_atomic,
)
from .downloads import download_response, file_sha256
from .origin import validate_origin_health, validate_update_health


# Default registry archive: the ``stable`` tag of the official package repository.
OFFICIAL_SOURCE = "https://github.com/guraltsev/pkg/archive/refs/tags/stable.zip"


@dataclass(frozen=True)
class RegistryPackage:
    """Describe one validated catalog selector and its install seed."""

    selector: str
    root: Path
    version_path: Path


@dataclass(frozen=True)
class RegistryState:
    """Report the active cached revision and the most recent sync outcome."""

    cache_root: Path
    revision: str | None = None
    source: str | None = None
    synchronized_at: str | None = None
    last_failure: str | None = None

    @property
    def tree_path(self) -> Path | None:
        """Return the active ``pkgs`` tree when one is available."""
        if self.revision is None:
            return None
        candidate = self.cache_root / "official" / "trees" / self.revision / "pkgs"
        return candidate if candidate.is_dir() else None


def registry_status(cache_root: Path) -> RegistryState:
    """Read cache state without contacting Git or creating directories."""
    state_path = Path(cache_root) / "official" / "state.toml"
    if not state_path.is_file():
        return RegistryState(Path(cache_root))
    values: dict[str, str] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return RegistryState(
        Path(cache_root),
        revision=values.get("revision") or None,
        source=values.get("source") or None,
        synchronized_at=values.get("synchronized_at") or None,
        last_failure=values.get("last_failure") or None,
    )


def validate_registry_tree(pkgs_root: Path) -> list[RegistryPackage]:
    """Validate a checked-out ``pkgs`` tree and return deterministic seeds.

    Parameters
    ----------
    pkgs_root : Path
        Directory containing one flat child directory per package selector.

    Returns
    -------
    list[RegistryPackage]
        Validated package seeds sorted case-insensitively by selector.

    Raises
    ------
    ConfigValidationError
        If the tree contains unsafe filesystem entries, selector collisions,
        malformed manifests, or unhealthy hook references.
    """
    pkgs_root = Path(pkgs_root)
    if not pkgs_root.is_dir() or pkgs_root.is_symlink():
        raise ConfigValidationError(f"Registry pkgs tree is not a directory: {pkgs_root}")
    packages: list[RegistryPackage] = []
    seen: dict[str, str] = {}
    for selector_path in sorted(pkgs_root.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        _reject_reparse(selector_path, "registry selector")
        if selector_path.name.startswith(".") and selector_path.is_file():
            continue
        if not selector_path.is_dir():
            raise ConfigValidationError(f"Registry selector is not a directory: {selector_path.name}")
        selector = selector_path.name
        key = selector.casefold()
        if key in seen:
            raise ConfigValidationError(
                f"Duplicate registry selector: {seen[key]} and {selector}"
            )
        seen[key] = selector
        versions = [
            item for item in selector_path.iterdir()
            if item.is_dir() and VERSION_DIR_NAME_RE.match(item.name)
        ]
        if len(versions) != 1:
            raise ConfigValidationError(
                f"Registry selector '{selector}' must contain exactly one version seed"
            )
        version_path = versions[0]
        _validate_tree_entries(version_path, version_path)
        manifest = version_path / "pkg.toml"
        if not manifest.is_file():
            raise ConfigValidationError(f"Registry seed has no pkg.toml: {version_path}")
        identity = PackageIdentity.from_version_path(
            selector_path, version_path, is_current=False
        )
        try:
            config, raw, _ = read_runtime_config(identity)
            inconsistencies = [
                message
                for message in check_metadata_consistency(identity, raw)
                if not message.startswith("Portable flag mismatch:")
            ]
            if inconsistencies:
                raise ConfigValidationError("; ".join(inconsistencies))
            health_errors = validate_origin_health(identity, config.get("origin"))
            health_errors.extend(validate_update_health(identity, config.get("update")))
            if health_errors:
                raise ConfigValidationError("; ".join(health_errors))
        except Exception as exc:
            if isinstance(exc, ConfigValidationError):
                raise ConfigValidationError(
                    f"Invalid registry seed for '{selector}': {exc}"
                ) from exc
            raise ConfigValidationError(f"Invalid registry seed {version_path}: {exc}") from exc
        packages.append(RegistryPackage(selector, selector_path, version_path))
    return packages


def search_registry(cache_root: Path, query: str = "") -> list[RegistryPackage]:
    """Return validated active-tree packages whose selector contains *query*.

    The search is case-insensitive, never uses the network, and returns every
    package for an empty query.
    """
    state = registry_status(cache_root)
    if state.tree_path is None:
        raise FileNotFoundError("No validated registry cache is available")
    needle = query.casefold()
    return [item for item in validate_registry_tree(state.tree_path) if needle in item.selector.casefold()]


def resolve_selector(cache_root: Path, selector: str) -> RegistryPackage:
    """Resolve one case-insensitive selector from the active validated tree."""
    matches = [item for item in search_registry(cache_root) if item.selector.casefold() == selector.casefold()]
    if not matches:
        raise ValueError(f"Registry package was not found: {selector}")
    if len(matches) > 1:
        raise ValueError(f"Registry selector is ambiguous: {selector}")
    return matches[0]


def sync_registry(
    cache_root: Path,
    *,
    source: str = OFFICIAL_SOURCE,
    offline: bool = False,
) -> ActionResult:
    """Download the registry archive and publish its validated ``pkgs`` tree.

    Parameters
    ----------
    cache_root : Path
        Registry cache directory from the manager configuration.
    source : str, default=OFFICIAL_SOURCE
        ``http(s)://`` or ``file:`` URL of a ZIP archive containing a ``pkgs``
        folder (a GitHub "archive" link, a mirror, or a file share).
    offline : bool, default=False
        Validate and report the existing cache without any download.

    Returns
    -------
    ActionResult
        ``synced``, ``current`` (the archive is the revision already cached),
        or ``offline``. On failure the previous validated tree stays active.
    """
    cache_root = Path(cache_root)
    current = registry_status(cache_root)
    if offline:
        if current.tree_path is None:
            return ActionResult(
                False,
                errors=["No validated registry cache is available in offline mode"],
                exit_code=EXIT_MUTATION_ERROR,
            )
        return ActionResult(True, status="offline", exit_code=EXIT_SUCCESS)

    official_root = cache_root / "official"
    work_parent = official_root / ".work"
    work_parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="sync-", dir=str(work_parent)) as work_name:
            work = Path(work_name)
            archive_path = work / "registry.zip"
            _download(source, archive_path)
            revision = _archive_revision(archive_path)

            # An archive that is already the active revision needs no rebuild;
            # just re-validate it and refresh the bookkeeping.
            if current.revision == revision and current.tree_path is not None:
                validate_registry_tree(current.tree_path)
                _write_state(official_root, current, source=source, last_failure=None)
                return ActionResult(True, status="current", exit_code=EXIT_SUCCESS)

            # Unpack only ``pkgs``, validate it fully, and only then publish.
            pkgs = work / "pkgs"
            _extract_pkgs(archive_path, pkgs)
            if not validate_registry_tree(pkgs):
                raise ConfigValidationError("Registry pkgs tree is empty")
            target = official_root / "trees" / revision
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pkgs, target / "pkgs")
        new_state = RegistryState(
            cache_root,
            revision=revision,
            source=source,
            synchronized_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_state(official_root, new_state, source=source, last_failure=None)
        return ActionResult(True, changed=True, status="synced", exit_code=EXIT_SUCCESS)
    except Exception as exc:
        _write_state(official_root, current, source=current.source or source, last_failure=str(exc))
        return ActionResult(
            False,
            warnings=["The previous validated registry tree remains active."],
            errors=[f"Registry synchronization failed: {exc}"],
            exit_code=EXIT_MUTATION_ERROR,
        )
    finally:
        shutil.rmtree(work_parent, ignore_errors=True)


def _download(source: str, destination: Path) -> None:
    """Download *source* (HTTP, HTTPS, or ``file:``) to *destination* with progress."""
    if urllib.parse.urlparse(source).scheme.lower() not in {"http", "https", "file"}:
        raise ConfigValidationError(
            f"Registry source must be an http(s):// or file: URL to a ZIP archive: {source}"
        )
    log_info(f"Downloading registry: {source}")
    with urllib.request.urlopen(source, timeout=60) as response:
        download_response(response, destination, label="Downloading registry")


def _archive_revision(archive_path: Path) -> str:
    """Return a stable 40-hex revision for a registry archive.

    GitHub archives record their commit ID in the ZIP comment; any other
    archive is identified by the first 40 hex digits of its SHA-256 digest.
    """
    with zipfile.ZipFile(archive_path) as archive:
        comment = archive.comment.decode("ascii", errors="ignore").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", comment):
        return comment
    return file_sha256(archive_path)[:40]


def _extract_pkgs(archive_path: Path, destination: Path) -> None:
    """Extract the archive's ``pkgs`` folder (directly or one level down) safely.

    Entries that are absolute, climb out with ``..``, are symlinks, or are
    named with a drive are rejected; everything outside ``pkgs`` is ignored.
    """
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = [(PurePosixPath(info.filename), info) for info in archive.infolist()]
        names = [parts for parts, _ in members]
        # GitHub wraps the repository in a single top-level folder.
        depth = 0 if any(parts.parts[:1] == ("pkgs",) for parts in names) else 1
        found = False
        for parts, info in members:
            if parts.parts[depth : depth + 1] != ("pkgs",):
                continue
            if parts.is_absolute() or ".." in parts.parts or ":" in info.filename:
                raise ConfigValidationError(f"Registry archive contains an unsafe path: {info.filename}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ConfigValidationError(f"Registry archive contains a symlink: {info.filename}")
            relative = PurePosixPath(*parts.parts[depth + 1 :])
            target = (root / relative).resolve() if relative.parts else root
            if not target.is_relative_to(root):
                raise ConfigValidationError(f"Registry archive contains an unsafe path: {info.filename}")
            found = True
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)
    if not found:
        raise ConfigValidationError("Registry archive has no pkgs folder")


def _validate_tree_entries(root: Path, path: Path) -> None:
    """Reject links, unsafe names, and manager-owned state recursively."""
    for entry in path.iterdir():
        _reject_reparse(entry, "registry tree")
        relative = entry.relative_to(root)
        if any(part in {".", ".."} or ":" in part for part in relative.parts):
            raise ConfigValidationError(f"Unsafe registry path: {relative}")
        if entry.name.casefold() in {"current", ".gupkg"}:
            raise ConfigValidationError(f"Registry tree contains reserved state: {relative}")
        if entry.is_dir():
            _validate_tree_entries(root, entry)


def _reject_reparse(path: Path, context: str) -> None:
    """Reject symbolic links and Windows reparse points before traversal."""
    if path.is_symlink():
        raise ConfigValidationError(f"{context} is a link: {path}")
    try:
        attributes = path.stat().st_file_attributes
    except (AttributeError, OSError):
        attributes = 0
    if attributes & 0x400:
        raise ConfigValidationError(f"{context} is a reparse point: {path}")


def _write_state(
    official_root: Path,
    state: RegistryState,
    *,
    source: str,
    last_failure: str | None,
) -> None:
    """Atomically publish cache metadata while retaining the active tree."""
    lines = [
        f"revision = {json.dumps(state.revision or '')}",
        f"source = {json.dumps(source)}",
        f"synchronized_at = {json.dumps(state.synchronized_at or '')}",
        f"last_failure = {json.dumps(last_failure or '')}",
    ]
    write_text_atomic(official_root / "state.toml", "\n".join(lines) + "\n")
