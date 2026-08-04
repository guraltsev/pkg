"""Discover manifest-backed packages below a bounded collection root.

Collections inspect direct children only unless a regular ``gupkg-dir.toml``
marker explicitly permits descent into a grouping directory.  The discovery
result preserves malformed manifests and inaccessible branches as diagnostics
so callers can report a complete inventory without guessing at arbitrary TOML
files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from .core import compare_package_versions, is_version_directory_name


@dataclass(frozen=True)
class DiscoveredManifest:
    """Describe one manifest found in a package version directory."""

    path: Path
    version_path: Path


@dataclass
class DiscoveredPackage:
    """Describe one package root and its manifest-bearing versions."""

    root: Path
    selector: str
    manifests: list[DiscoveredManifest]
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    """Represent packages and non-fatal traversal diagnostics for one root."""

    root: Path
    packages: list[DiscoveredPackage] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    complete: bool = True


def discover_collection(root: Path, *, max_depth: int = 8) -> Inventory:
    """Discover manifest-backed packages below ``root``.

    Parameters
    ----------
    root : Path
        Directory from which shallow collection discovery begins.
    max_depth : int, default=8
        Maximum number of directory edges traversed through marked groupings.

    Returns
    -------
    Inventory
        Deterministically ordered packages and any incomplete-scan diagnostics.

    Raises
    ------
    ValueError
        If ``root`` is not an existing directory or ``max_depth`` is negative.
    """
    if max_depth < 0:
        raise ValueError("--max-depth must be zero or greater")
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"Collection root does not exist or is not a directory: {resolved_root}")
    inventory = Inventory(root=resolved_root)
    visited = {os.path.normcase(str(resolved_root))}

    def scan(directory: Path, depth: int) -> None:
        """Inspect immediate children while keeping all failures local to a branch."""
        try:
            children = sorted(directory.iterdir(), key=lambda path: (path.name.casefold(), path.name))
        except OSError as exc:
            inventory.complete = False
            inventory.diagnostics.append(f"Could not scan {directory}: {exc}")
            return
        for child in children:
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
                manifests = [
                    DiscoveredManifest(version / "pkg.toml", version)
                    for version in child.iterdir()
                    if version.is_dir()
                    and not version.is_symlink()
                    and is_version_directory_name(version.name)
                    and (version / "pkg.toml").is_file()
                ]
                marker = child / "gupkg-dir.toml"
            except OSError as exc:
                inventory.complete = False
                inventory.diagnostics.append(f"Could not inspect {child}: {exc}")
                continue
            if manifests:
                relative = child.relative_to(resolved_root).as_posix()
                package = DiscoveredPackage(child, relative, manifests)
                if marker.is_file():
                    package.diagnostics.append("Layout conflict: package root also contains gupkg-dir.toml")
                inventory.packages.append(package)
                continue
            if marker.is_file():
                if depth >= max_depth:
                    inventory.complete = False
                    inventory.diagnostics.append(f"Maximum depth reached at marked grouping directory: {child}")
                    continue
                resolved = child.resolve()
                key = os.path.normcase(str(resolved))
                if key in visited:
                    inventory.complete = False
                    inventory.diagnostics.append(f"Skipped already visited grouping directory: {child}")
                    continue
                visited.add(key)
                scan(child, depth + 1)

    scan(resolved_root, 0)
    inventory.packages.sort(key=lambda package: (package.selector.casefold(), package.selector))
    for package in inventory.packages:
        # Version names are semantic package versions, not ordinary filenames.
        package.manifests.sort(
            key=cmp_to_key(
                lambda left, right: compare_package_versions(
                    left.version_path.name, right.version_path.name
                )
            )
        )
    return inventory


def select_package(inventory: Inventory, selector: str) -> DiscoveredPackage:
    """Select a package by canonical selector or an unambiguous basename."""
    folded = selector.casefold()
    exact = [package for package in inventory.packages if package.selector.casefold() == folded]
    matches = exact or [package for package in inventory.packages if package.root.name.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    choices = ", ".join(package.selector for package in matches or inventory.packages)
    if matches:
        raise ValueError(f"Package selector is ambiguous: {selector}; choose one of: {choices}")
    raise ValueError(f"Package selector was not found: {selector}; available: {choices or '(none)'}")
