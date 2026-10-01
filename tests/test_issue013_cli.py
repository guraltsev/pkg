"""Cover the issue 013 package command and configuration-repair contract."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
import tomllib

from gupkg import cli


def _version_directory(root: Path) -> Path:
    """Create a minimal valid package layout for command tests."""
    version = root / "Example" / "v1.2.3.l1"
    (version / "App").mkdir(parents=True)
    return version


def test_removed_grammar_is_rejected_by_the_single_parser() -> None:
    """The old nested upgrade grammar is not accepted as a compatibility alias."""
    try:
        cli.main(["upgrade", "check"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - argparse must reject this input
        raise AssertionError("removed grammar was accepted")


def test_config_fix_validates_before_mutating_an_existing_document(tmp_path: Path) -> None:
    """Unknown canonical keys fail without creating a backup or changing text."""
    version = _version_directory(tmp_path)
    destination = version / "pkg.toml"
    original = (
        'name = "Example"\nversion = "1.2.3"\nlocalVersion = 1\n'
        "only_portable = false\nunknown = true\n"
    )
    destination.write_text(original, encoding="utf-8")

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "config-fix", str(version)])

    document = tomllib.loads(output.getvalue())
    assert code == 2
    assert document["ok"] is False
    assert destination.read_text(encoding="utf-8") == original
    assert list(version.glob("pkg.toml.bak.*")) == []


def test_config_fix_creates_timestamped_backup_only_when_content_changes(tmp_path: Path) -> None:
    """A successful synchronization creates the required sibling backup."""
    version = _version_directory(tmp_path)
    destination = version / "pkg.toml"
    destination.write_text(
        'name = "Wrong"\nversion = "0.0.0"\nlocalVersion = 0\nonly_portable = false\n',
        encoding="utf-8",
    )

    assert cli.main(["config-fix", str(version)]) == 0
    backups = list(version.glob("pkg.toml.bak.*"))
    assert len(backups) == 1
    parsed = tomllib.loads(destination.read_text(encoding="utf-8"))
    assert parsed["name"] == "Example"
    assert parsed["version"] == "1.2.3"
