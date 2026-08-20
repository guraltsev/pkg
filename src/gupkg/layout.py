"""Resolve package layouts and maintain the active-version junction.

User paths may identify a version directory, a package root, or its ``current``
junction. Resolution always produces one directory-derived package identity,
and junction updates refuse unsafe targets outside the owning package root.

Implementation Approach
-----------------------
Paths are classified without prematurely dereferencing ``current``. Activation
prepares and verifies a temporary junction before atomically replacing the
active path, with rollback for interrupted replacements.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple

from .core import (
    PackageIdentity,
    Scope,
    compare_package_versions,
    is_version_directory_name,
    log_info,
    log_warning,
    normalize_path,
)
from .windows import create_junction, get_junction_target, is_junction


def compute_scope_paths(scope: Scope) -> Dict[str, Path]:
    """Resolve the filesystem locations needed by one install scope.

    Parameters
    ----------
    scope : Scope
        Installation scope for which paths should be calculated.

    Returns
    -------
    Dict[str, Path]
        A small mapping containing only the path values the install flow
        actually uses:

        - ``shortcut_root``: Start Menu root for generated shortcuts.
        - ``bin_dir``: Directory for generated wrapper files.

    Raises
    ------
    ValueError
        If required environment variables such as ``APPDATA`` or
            ``PROGRAMDATA`` are missing.

    """
    if scope == Scope.USER:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ValueError(
                "APPDATA is not set; cannot compute User-scope shortcut directory."
            )
        userprofile = os.environ.get("USERPROFILE")
        if not userprofile:
            raise ValueError(
                "USERPROFILE is not set; cannot compute User-scope bin directory."
            )
        return {
            "shortcut_root": Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "opt",
            "bin_dir": Path(userprofile) / "bin",
        }

    programdata = os.environ.get("PROGRAMDATA")
    if not programdata:
        raise ValueError(
            "PROGRAMDATA is not set; cannot compute Machine-scope shortcut directory."
        )
    systemdrive = os.environ.get("SYSTEMDRIVE")
    if not systemdrive:
        raise ValueError(
            "SYSTEMDRIVE is not set; cannot compute Machine-scope bin directory."
        )
    if len(systemdrive) == 2 and systemdrive[0].isalpha() and systemdrive[1] == ":":
        systemdrive = systemdrive + "\\"
    return {
        "shortcut_root": Path(programdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "opt",
        "bin_dir": Path(systemdrive) / "bin",
    }


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class CurrentInspection:
    """Describe the activation entry owned by one package root."""

    status: str
    version_path: Path | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def inspect_current(package_root: Path) -> CurrentInspection:
    """Inspect ``current`` without selecting a fallback version directory.

    Parameters
    ----------
    package_root : Path
        Package root whose activation entry should be inspected.

    Returns
    -------
    CurrentInspection
        ``installed`` for a valid activation, ``not-installed`` when the
        entry is absent, or ``broken`` when an entry exists but is unsafe.
    """
    current_path = package_root / "current"
    if not os.path.lexists(str(current_path)):
        return CurrentInspection("not-installed")
    if not is_junction(current_path):
        return CurrentInspection(
            "broken", diagnostics=(f'"current" is not a junction: {current_path}',)
        )
    target = get_junction_target(current_path)
    if target is None:
        return CurrentInspection(
            "broken", diagnostics=(f'Could not resolve "current": {current_path}',)
        )
    try:
        resolved_root = package_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root):
            return CurrentInspection(
                "broken",
                diagnostics=(f'"current" points outside package root: {target}',),
            )
    except OSError as exc:
        return CurrentInspection("broken", diagnostics=(f"Could not inspect current target: {exc}",))
    if not resolved_target.is_dir() or not is_version_directory_name(resolved_target.name):
        return CurrentInspection(
            "broken", diagnostics=(f'"current" target is not a version directory: {target}',)
        )
    if not (resolved_target / "pkg.toml").is_file():
        return CurrentInspection(
            "broken", diagnostics=(f'"current" target has no pkg.toml: {resolved_target}',)
        )
    return CurrentInspection("installed", resolved_target)


def _warn_if_output_path_is_unusual(
    kind: str, default_root: Path, expanded_name: str, final_path: Path
) -> None:
    """Warn when a shortcut/bin output lands outside its default root.

    Relative nested paths inside the default root are allowed without warning.
    Absolute names and escaping parent traversal remain allowed, but they are
    noisy enough that install should call them out explicitly.
    """
    looks_absolute = (
        expanded_name.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(expanded_name) is not None
    )

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
        outside_default_root = outside_default_root or not final_path.resolve(
            strict=False
        ).is_relative_to(default_root.resolve(strict=False))
    except OSError:
        outside_default_root = True

    if looks_absolute or outside_default_root:
        destination = expanded_name if looks_absolute else str(final_path)
        log_warning(
            f"{kind} output resolves outside the default {kind} root; this is allowed but unusual: {destination}"
        )


def _current_version_matches(package_root: Path, version_path: Path) -> bool:
    """Return whether ``package_root/current`` points at ``version_path``.

    Parameters
    ----------
    package_root : Path
        Package root that may contain the ``current`` junction.
    version_path : Path
        Concrete version directory to compare against.

    Returns
    -------
    bool
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


def _resolve_unique_version_directory(package_root: Path) -> Path:
    """Return the only version directory under a package root without ``current``.

    Parameters
    ----------
    package_root : Path
        Package root that is missing a ``current`` junction.

    Returns
    -------
    Path
        The single version directory found directly under *package_root*.

    Raises
    ------
    ValueError
        If there are no version directories or if more than one
            version directory exists and the caller must disambiguate.

    """
    version_directories = [
        child
        for child in package_root.iterdir()
        if child.is_dir() and is_version_directory_name(child.name)
    ]

    if len(version_directories) == 1:
        return version_directories[0]

    if not version_directories:
        raise ValueError(
            f'Package root has no "current" junction and no version directory to use: {package_root}'
        )

    version_list = ", ".join(sorted(child.name for child in version_directories))
    raise ValueError(
        f'Package root has no "current" junction and contains multiple version directories: {package_root}; '
        f"found: {version_list}. Pass an explicit version directory instead."
    )


def resolve_input_path(raw_path: Path) -> Tuple[PackageIdentity, bool]:
    """Resolve a user-supplied path to one concrete package version.

    Parameters
    ----------
    raw_path : Path
        User-supplied path that may point at a version directory, a
            ``current`` junction, or the package root.

    Returns
    -------
    Tuple[PackageIdentity, bool]
        Tuple ``(identity, installing_from_current)`` where *identity*
        describes the concrete version directory to operate on and
        *installing_from_current* reports whether the caller pointed at
        ``current`` or the package root instead of a version directory.
        A package root with no ``current`` junction is accepted when it
        contains exactly one version directory.

    Raises
    ------
    ValueError
        If the path does not match a supported package layout.

    """
    # Normalize lexically so a trailing ``current`` path component is preserved
    # instead of being dereferenced before package layout classification.
    candidate = Path(os.path.abspath(os.fspath(raw_path.expanduser())))

    if is_version_directory_name(candidate.name):
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"Version directory does not exist: {candidate}")
        package_root = candidate.parent
        return (
            PackageIdentity.from_version_path(
                package_root,
                candidate,
                is_current=_current_version_matches(package_root, candidate),
            ),
            False,
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
            raise ValueError(
                f'Could not resolve "current" junction target: {candidate}'
            )
        resolved_target = normalize_path(target)
        if not resolved_target.is_dir():
            raise ValueError(
                f'"current" junction target is not a directory: {resolved_target}; source={candidate}, raw_target={target}'
            )
        return (
            PackageIdentity.from_version_path(
                candidate.parent, resolved_target, is_current=True
            ),
            True,
        )

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Package root does not exist: {candidate}")
    current_path = candidate / "current"
    if os.path.lexists(str(current_path)):
        if not is_junction(current_path):
            raise ValueError(
                f'"current" path exists but is not a valid junction: {current_path}; '
                f"exists={current_path.exists()}, is_dir={current_path.is_dir()}, parent={current_path.parent}"
            )
        target = get_junction_target(current_path)
        if target is None:
            raise ValueError(
                f'Could not resolve "current" junction target: {current_path}'
            )
        resolved_target = normalize_path(target)
        if not resolved_target.is_dir():
            raise ValueError(
                f'"current" junction target is not a directory: {resolved_target}; source={current_path}, raw_target={target}'
            )
        return PackageIdentity.from_version_path(
            candidate, resolved_target, is_current=True
        ), True

    # An uninstalled package root can still be useful if it contains exactly
    # one version directory. In that case we operate on the version directory
    # directly and let Install create ``current`` later.
    version_path = _resolve_unique_version_directory(candidate)
    return PackageIdentity.from_version_path(
        candidate, version_path, is_current=False
    ), False


def update_current_junction_if_needed(
    identity: PackageIdentity, *, allow_downgrade: bool = False
) -> bool:
    r"""Update ``<package>\current`` unless a newer version should win.

    When this function runs for the version that is already active, it may
    still refresh ``current`` by recreating the junction. That behavior is
    intentional: install uses reruns as a repair path for external state,
    so same-version targets are not treated as a junction no-op here.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that should become or remain ``current``.
    allow_downgrade : bool
        Whether to allow replacing ``current`` when it already
            points to a newer version. Same-version targets may still
            refresh ``current`` without the override.

    Returns
    -------
    bool
        ``True`` when ``current`` was recreated or repointed; ``False``
        only when ``current`` was intentionally left untouched because a
        newer version was already active.

    Raises
    ------
    ValueError
        If the existing ``current`` path is unsafe or malformed.
    RuntimeError
        If the junction replacement fails.

    """
    current_path = identity.package_root / "current"
    desired_target = identity.version_path

    if not desired_target.exists() or not desired_target.is_dir():
        raise RuntimeError(
            f"Junction target does not exist or is not a directory: {desired_target}"
        )

    if os.path.lexists(str(current_path)):
        if not is_junction(current_path):
            raise ValueError(
                f"{current_path} exists but is not a junction. Aborting all operations."
            )

        current_target = get_junction_target(current_path)
        if current_target is None:
            raise ValueError(
                f"{current_path} is a junction but its target is not resolvable. Aborting."
            )

        current_target = current_target.resolve()
        if not current_target.is_dir():
            log_info(f"JUNCTION: stale current target detected: {current_target}")
        else:
            if current_target.parent != identity.package_root:
                raise ValueError(
                    f"{current_path} is a junction but its target {current_target} "
                    f"is not under {identity.package_root}. Aborting."
                )

            current_version = current_target.name
            log_info(f"'current' junction version: {current_version}")
            comparison = compare_package_versions(
                identity.version_string, current_version
            )
            # Same-version reinstalls are a supported refresh path. Only
            # keep the existing junction untouched when it points to a
            # newer version and --allow-downgrade was not requested.
            if not allow_downgrade and comparison < 0:
                log_info(
                    f"JUNCTION: keeping current ({current_version} > {identity.version_string})"
                )
                return False
            if allow_downgrade:
                log_info(
                    f"JUNCTION: --allow-downgrade: updating current to {identity.version_string}"
                )

    # Refreshing the currently active version may still recreate
    # ``current`` so install can reassert the active package view.
    new_path = current_path.with_name(
        f"{current_path.name}.__new__.{uuid.uuid4().hex[:8]}"
    )
    old_path = current_path.with_name(
        f"{current_path.name}.__old__.{uuid.uuid4().hex[:8]}"
    )
    moved_current = False
    try:
        if os.path.lexists(str(new_path)):
            if is_junction(new_path):
                os.rmdir(str(new_path))
            else:
                raise RuntimeError(
                    f"Temporary junction path already exists and is unsafe to replace: {new_path}"
                )

        create_junction(new_path, desired_target)
        new_target = get_junction_target(new_path)
        if new_target is None or normalize_path(new_target) != normalize_path(
            desired_target
        ):
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
        if (
            not os.path.lexists(str(current_path))
            and moved_current
            and os.path.lexists(str(old_path))
        ):
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
