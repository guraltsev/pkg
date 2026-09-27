"""Locate the checked-in versioned gupkg runtime used by tests."""

from __future__ import annotations

from pathlib import Path


def find_runtime_directory(repository_root: Path) -> Path:
    """Return the sole versioned source directory that contains the gupkg CLI."""
    source_root = repository_root / "src"
    candidates = sorted(
        path
        for path in source_root.iterdir()
        if path.is_dir() and (path / "gupkg" / "gupkg.py").is_file()
    )
    if len(candidates) != 1:
        found = ", ".join(path.name for path in candidates) or "none"
        raise RuntimeError(
            f"Expected one versioned gupkg runtime below {source_root}, found: {found}"
        )
    return candidates[0]
