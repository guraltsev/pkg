"""Locate the canonical source root used by legacy fixture tests."""

from __future__ import annotations

from pathlib import Path


def find_runtime_directory(repository_root: Path) -> Path:
    """Return ``src`` when it contains the installable ``gupkg`` package."""
    source_root = repository_root / "src"
    if not (source_root / "gupkg" / "gupkg.py").is_file():
        raise RuntimeError(f"Canonical gupkg source is missing below {source_root}")
    return source_root
