"""Cover observable gupkg collection discovery and selector naming.

The tests create real package-shaped directories and exercise only the public
discovery API; Textual, network update sources, and Windows junctions are out
of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gupkg.collection import discover_collection


def _manifest(root: Path, selector: str, version: str = "v1.0.0.l1") -> None:
    """Create the smallest manifest-backed package shape for a test collection."""
    manifest = root / selector / version / "pkg.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('name = "example"\nversion = "1.0.0"\nlocalVersion = 1\n')


def test_discovery_visits_only_marked_groupings_and_keeps_malformed_manifest(tmp_path: Path) -> None:
    """Collection discovery finds shallow packages and marker-authorized nested packages."""
    _manifest(tmp_path, "vscode")
    _manifest(tmp_path, "ignored/source")
    grouping = tmp_path / "editors"
    grouping.mkdir()
    (grouping / "gupkg-dir.toml").write_text("")
    _manifest(grouping, "vim")

    inventory = discover_collection(tmp_path)

    assert [package.selector for package in inventory.packages] == ["editors/vim", "vscode"]
    assert inventory.complete


def test_same_basename_in_two_groupings_yields_distinct_canonical_selectors(tmp_path: Path) -> None:
    """Packages sharing a basename are reported under their full grouping paths."""
    for group in ("stable", "preview"):
        directory = tmp_path / group
        directory.mkdir()
        (directory / "gupkg-dir.toml").write_text("")
        _manifest(directory, "vscode")

    inventory = discover_collection(tmp_path)

    assert [package.selector for package in inventory.packages] == ["preview/vscode", "stable/vscode"]
