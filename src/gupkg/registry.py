"""Synchronize and query the official, read-only ``pkgs/`` registry tree.

The registry module treats Git as a transport boundary and validates the
checked-out tree before making it visible to callers. It never imports or
executes package-local hooks; payload acquisition remains the responsibility
of the ordinary package installation workflow.

Usage and API
-------------
Call ``sync_registry(...)`` to refresh a cache, ``registry_status(...)`` to
inspect its active revision, and ``search_registry(...)`` or
``resolve_selector(...)`` for deterministic offline catalog operations.

Implementation Approach
-----------------------
Stable-tag resolution and sparse checkout write to a disposable directory.
The resulting ``pkgs`` tree is validated with the existing manifest
normalizer, then renamed into a revision-addressed cache and published by an
atomic state file update. Failed work is discarded while the previous active
revision remains usable.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .configuration import check_metadata_consistency, read_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    PackageIdentity,
    VERSION_DIR_NAME_RE,
    write_text_atomic,
)
from .origin import validate_origin_health, validate_update_health


OFFICIAL_REPOSITORY = "https://github.com/guraltsev/pkg.git"
STABLE_REF = "refs/tags/stable"


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


def default_registry_cache() -> Path:
    """Return the per-user default registry cache without creating it."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    return Path(local_app_data) / "gupkg" / "registry"


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


def search_registry(
    cache_root: Path, query: str = "", *, installed: Iterable[str] | None = None
) -> list[RegistryPackage]:
    """Search the validated active tree without using the network."""
    state = registry_status(cache_root)
    if state.tree_path is None:
        raise FileNotFoundError("No validated registry cache is available")
    packages = validate_registry_tree(state.tree_path)
    needle = query.casefold()
    installed_keys = {item.casefold() for item in installed or ()}
    if installed is not None:
        packages = [item for item in packages if (item.selector.casefold() in installed_keys) == True]
    if needle:
        packages = [item for item in packages if needle in item.selector.casefold()]
    return packages


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
    offline: bool = False,
    repository: str = OFFICIAL_REPOSITORY,
    stable_ref: str = STABLE_REF,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ActionResult:
    """Synchronize the official ``pkgs`` subtree into an atomic cache."""
    cache_root = Path(cache_root)
    current = registry_status(cache_root)
    if offline:
        if current.tree_path is None:
            return ActionResult(False, errors=["No validated registry cache is available in offline mode"], exit_code=EXIT_MUTATION_ERROR)
        return ActionResult(True, status="offline", exit_code=EXIT_SUCCESS)
    official_root = cache_root / "official"
    trees_root = official_root / "trees"
    work_parent = official_root / ".work"
    work_parent.mkdir(parents=True, exist_ok=True)
    try:
        revision = _resolve_revision(repository, stable_ref, runner)
        if current.revision == revision and current.tree_path is not None:
            validate_registry_tree(current.tree_path)
            _write_state(official_root, current, source=repository, last_failure=None)
            shutil.rmtree(work_parent, ignore_errors=True)
            return ActionResult(True, status="current", exit_code=EXIT_SUCCESS)
        with tempfile.TemporaryDirectory(prefix="sync-", dir=str(work_parent)) as work_name:
            work = Path(work_name)
            checkout = _sparse_checkout(repository, revision, work, runner)
            packages = validate_registry_tree(checkout / "pkgs")
            if not packages:
                raise ConfigValidationError("Registry pkgs tree is empty")
            target = trees_root / revision
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(checkout / "pkgs", target / "pkgs")
        new_state = RegistryState(
            cache_root,
            revision=revision,
            source=repository,
            synchronized_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_state(official_root, new_state, source=repository, last_failure=None)
        shutil.rmtree(work_parent, ignore_errors=True)
        return ActionResult(True, changed=True, status="synced", exit_code=EXIT_SUCCESS)
    except Exception as exc:
        _write_state(official_root, current, source=current.source or repository, last_failure=str(exc))
        shutil.rmtree(work_parent, ignore_errors=True)
        return ActionResult(
            False,
            warnings=["The previous validated registry tree remains active."],
            errors=[f"Registry synchronization failed: {exc}"],
            exit_code=EXIT_MUTATION_ERROR,
        )


def _resolve_revision(repository: str, stable_ref: str, runner: Callable[..., Any]) -> str:
    """Resolve the stable ref to one full commit ID."""
    result = runner(
        ["git", "ls-remote", "--exit-code", repository, stable_ref],
        capture_output=True,
        text=True,
        check=True,
    )
    revision = result.stdout.split()[0] if result.stdout else ""
    if len(revision) != 40 or any(character not in "0123456789abcdefABCDEF" for character in revision):
        raise RuntimeError("Stable registry ref did not resolve to a full commit ID")
    return revision.lower()


def _sparse_checkout(repository: str, revision: str, work: Path, runner: Callable[..., Any]) -> Path:
    """Checkout only ``pkgs`` into a disposable Git worktree."""
    checkout = work / "checkout"
    runner(
        ["git", "clone", "--filter=blob:none", "--no-checkout", repository, str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    runner(["git", "-C", str(checkout), "sparse-checkout", "init", "--no-cone"], capture_output=True, text=True, check=True)
    runner(["git", "-C", str(checkout), "sparse-checkout", "set", "pkgs"], capture_output=True, text=True, check=True)
    runner(["git", "-C", str(checkout), "checkout", "--detach", revision], capture_output=True, text=True, check=True)
    return checkout


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
