"""Cover observable gupkg collection discovery and selector behavior.

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

from gupkg.collection import discover_collection, select_package


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


def test_selector_requires_a_canonical_name_when_a_short_name_is_ambiguous(tmp_path: Path) -> None:
    """An ambiguous basename reports choices instead of selecting an arbitrary package."""
    for group in ("stable", "preview"):
        directory = tmp_path / group
        directory.mkdir()
        (directory / "gupkg-dir.toml").write_text("")
        _manifest(directory, "vscode")

    inventory = discover_collection(tmp_path)

    try:
        select_package(inventory, "vscode")
    except ValueError as exc:
        assert "stable/vscode" in str(exc)
        assert "preview/vscode" in str(exc)
    else:
        raise AssertionError("ambiguous short selector was accepted")
