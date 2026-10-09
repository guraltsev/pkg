"""Resolve package layouts and maintain the active-version junction.

User paths may identify a version directory, a package root, or its ``current``
junction. Resolution always produces one directory-derived package identity,
and junction updates refuse unsafe targets outside the owning package root.

Usage and API
-------------
``resolve_input_path(...)`` turns a user path into a :class:`PackageIdentity`,
``inspect_current(...)`` reports a package root's activation without choosing
a fallback, and ``update_current_junction_if_needed(...)`` activates a
version. ``compute_scope_paths(...)`` returns the default shortcut and wrapper
locations of one scope.

Implementation Approach
-----------------------
Paths are classified lexically so a trailing ``current`` component is not
dereferenced prematurely. Activation prepares and verifies a temporary
junction before atomically replacing the active path, with rollback for
interrupted replacements.
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


def resolve_input_path(raw_path: Path) -> Tuple[PackageIdentity, bool]:
    """Resolve a user-supplied path to one concrete package version.

    Parameters
    ----------
    raw_path : Path
        A version directory, a ``current`` junction, or a package root.

    Returns
    -------
    Tuple[PackageIdentity, bool]
        ``(identity, installing_from_current)``, where the flag reports that
        the caller named ``current`` or a package root with ``current`` rather
        than a version directory. A package root without ``current`` is
        accepted when it contains exactly one version directory.

    Raises
    ------
    ValueError
        If the path does not match a supported package layout.

    """
    # Normalize lexically so a trailing ``current`` path component is preserved
    # instead of being dereferenced before package layout classification.
    candidate = Path(os.path.abspath(os.fspath(Path(raw_path).expanduser())))

    if is_version_directory_name(candidate.name):
        if not candidate.is_dir():
            raise ValueError(f"Version directory does not exist: {candidate}")
        package_root = candidate.parent
        identity = PackageIdentity.from_version_path(
            package_root, candidate, is_current=_current_target(package_root) == normalize_path(candidate)
        )
        return identity, False

    if candidate.name.lower() == "current":
        if not candidate.exists():
            raise ValueError(f'"current" path does not exist: {candidate}')
        return PackageIdentity.from_version_path(
            candidate.parent, _read_current(candidate), is_current=True
        ), True

    if not candidate.is_dir():
        raise ValueError(f"Package root does not exist: {candidate}")
    current_path = candidate / "current"
    if os.path.lexists(current_path):
        return PackageIdentity.from_version_path(
            candidate, _read_current(current_path), is_current=True
        ), True

    # An uninstalled package root is still usable when it holds exactly one
    # version directory; install will create ``current`` later.
    versions = sorted(
        child for child in candidate.iterdir() if child.is_dir() and is_version_directory_name(child.name)
    )
    if not versions:
        raise ValueError(
            f'Package root has no "current" junction and no version directory to use: {candidate}'
        )
    if len(versions) > 1:
        raise ValueError(
            f'Package root has no "current" junction and contains multiple version directories: {candidate}; '
            f"found: {', '.join(path.name for path in versions)}. Pass an explicit version directory instead."
        )
    return PackageIdentity.from_version_path(candidate, versions[0], is_current=False), False


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
    if not os.path.lexists(current_path):
        return CurrentInspection("not-installed")

    def broken(message: str) -> CurrentInspection:
        """Report an unusable activation entry."""
        return CurrentInspection("broken", diagnostics=(message,))

    if not is_junction(current_path):
        return broken(f'"current" is not a junction: {current_path}')
    target = get_junction_target(current_path)
    if target is None:
        return broken(f'Could not resolve "current": {current_path}')
    try:
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(package_root.resolve()):
            return broken(f'"current" points outside package root: {target}')
    except OSError as exc:
        return broken(f"Could not inspect current target: {exc}")
    if not resolved_target.is_dir() or not is_version_directory_name(resolved_target.name):
        return broken(f'"current" target is not a version directory: {target}')
    if not (resolved_target / "pkg.toml").is_file():
        return broken(f'"current" target has no pkg.toml: {resolved_target}')
    return CurrentInspection("installed", resolved_target)


def update_current_junction_if_needed(
    identity: PackageIdentity, *, allow_downgrade: bool = False
) -> bool:
    r"""Point ``<package>\current`` at *identity* unless a newer version should win.

    Re-activating the version that is already active still recreates the
    junction: install reruns are the supported repair path.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that should become or remain ``current``.
    allow_downgrade : bool
        Whether to replace ``current`` when it already points to a newer
        version.

    Returns
    -------
    bool
        ``True`` when ``current`` was recreated or repointed; ``False`` only
        when it was left untouched because a newer version is active.

    Raises
    ------
    ValueError
        If the existing ``current`` path is unsafe or malformed.
    RuntimeError
        If the junction replacement fails.

    """
    current_path = identity.package_root / "current"
    desired_target = identity.version_path
    if not desired_target.is_dir():
        raise RuntimeError(f"Junction target does not exist or is not a directory: {desired_target}")

    # Inspect the existing activation: it must be a junction inside this
    # package root, and a newer active version wins without --allow-downgrade.
    if os.path.lexists(current_path):
        if not is_junction(current_path):
            raise ValueError(f"{current_path} exists but is not a junction. Aborting all operations.")
        current_target = get_junction_target(current_path)
        if current_target is None:
            raise ValueError(f"{current_path} is a junction but its target is not resolvable. Aborting.")
        current_target = current_target.resolve()
        if not current_target.is_dir():
            log_info(f"JUNCTION: stale current target detected: {current_target}")
        elif current_target.parent != identity.package_root.resolve():
            raise ValueError(
                f"{current_path} is a junction but its target {current_target} "
                f"is not under {identity.package_root}. Aborting."
            )
        else:
            log_info(f"'current' junction version: {current_target.name}")
            if compare_package_versions(identity.version_string, current_target.name) < 0:
                if not allow_downgrade:
                    log_info(f"JUNCTION: keeping current ({current_target.name} > {identity.version_string})")
                    return False
                log_info(f"JUNCTION: --allow-downgrade: updating current to {identity.version_string}")

    # Build and verify a temporary junction, then swap it in. The old
    # junction is parked under a unique name so a failed swap can restore it.
    suffix = uuid.uuid4().hex[:8]
    new_path = current_path.with_name(f"current.__new__.{suffix}")
    old_path = current_path.with_name(f"current.__old__.{suffix}")
    try:
        create_junction(new_path, desired_target)
        new_target = get_junction_target(new_path)
        if new_target is None or normalize_path(new_target) != normalize_path(desired_target):
            raise RuntimeError(
                f"Temporary junction verification failed: expected {desired_target}, got {new_target}"
            )
        if os.path.lexists(current_path):
            os.replace(current_path, old_path)
        os.replace(new_path, current_path)
        if os.path.lexists(old_path):
            os.rmdir(old_path)
    finally:
        if os.path.lexists(new_path):
            try:
                os.rmdir(new_path)
            except OSError:
                pass
        if os.path.lexists(old_path) and not os.path.lexists(current_path):
            try:
                os.replace(old_path, current_path)
            except OSError:
                pass
    log_info(f"JUNCTION: created: {current_path.name} -> {desired_target}")
    return True


def compute_scope_paths(scope: Scope) -> Dict[str, Path]:
    """Resolve the default shortcut and wrapper locations for one install scope.

    Parameters
    ----------
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``.

    Returns
    -------
    Dict[str, Path]
        ``shortcut_root`` (Start Menu ``opt`` folder) and ``bin_dir``.

    Raises
    ------
    ValueError
        If a required environment variable such as ``APPDATA`` is missing.

    """
    def env(name: str, purpose: str) -> str:
        """Return a required environment variable or explain why it is needed."""
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"{name} is not set; cannot compute {purpose}.")
        return value

    start_menu = Path("Microsoft") / "Windows" / "Start Menu" / "opt"
    if scope == Scope.USER:
        return {
            "shortcut_root": Path(env("APPDATA", "User-scope shortcut directory")) / start_menu,
            "bin_dir": Path(env("USERPROFILE", "User-scope bin directory")) / "bin",
        }
    shortcut_root = Path(env("PROGRAMDATA", "Machine-scope shortcut directory")) / start_menu
    system_drive = env("SYSTEMDRIVE", "Machine-scope bin directory")
    if len(system_drive) == 2 and system_drive[0].isalpha() and system_drive[1] == ":":
        system_drive += "\\"
    return {"shortcut_root": shortcut_root, "bin_dir": Path(system_drive) / "bin"}


def warn_if_output_path_is_unusual(
    kind: str, default_root: Path, expanded_name: str, final_path: Path
) -> None:
    """Warn when a shortcut or wrapper output lands outside its default root.

    Relative nested paths inside the default root are silent. Absolute names
    and escaping ``..`` traversal remain allowed, but are called out.

    Parameters
    ----------
    kind : str
        ``shortcut`` or ``bin``, used in the warning.
    default_root : Path
        The scope's default output root.
    expanded_name : str
        The configured name after variable expansion.
    final_path : Path
        The output path that will be written.
    """
    looks_absolute = expanded_name.startswith(("/", "\\")) or re.match(
        r"^[A-Za-z]:[\\/]", expanded_name
    ) is not None

    # Walk the relative segments; going above the root at any point escapes it
    # even when later segments come back down.
    depth = 0
    escapes = False
    for segment in re.split(r"[\\/]+", expanded_name):
        if segment in ("", "."):
            continue
        depth += -1 if segment == ".." else 1
        if depth < 0:
            escapes = True
            break
    try:
        escapes = escapes or not final_path.resolve(strict=False).is_relative_to(
            default_root.resolve(strict=False)
        )
    except OSError:
        escapes = True
    if looks_absolute or escapes:
        destination = expanded_name if looks_absolute else str(final_path)
        log_warning(
            f"{kind} output resolves outside the default {kind} root; this is allowed but unusual: {destination}"
        )


def _read_current(current_path: Path) -> Path:
    """Return the version directory an existing ``current`` junction targets."""
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
            f'"current" junction target is not a directory: {resolved_target}; '
            f"source={current_path}, raw_target={target}"
        )
    return resolved_target


def _current_target(package_root: Path) -> Path | None:
    """Return the normalized target of a package root's ``current``, if readable."""
    current_path = package_root / "current"
    if not current_path.exists() or not is_junction(current_path):
        return None
    target = get_junction_target(current_path)
    try:
        return normalize_path(target) if target is not None else None
    except OSError:
        return None
