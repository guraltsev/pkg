"""Cover manager configuration validation and scoped local inventory.

These tests exercise real TOML files and temporary package layouts. Network
providers, Textual, and filesystem permission simulation are out of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gupkg.core import ConfigValidationError, Scope
from gupkg.manager import discover_manager, load_manager_config, select_target


def _write_config(path: Path, system: str, user: str, *, extra: str = "") -> None:
    """Create a manager configuration with the requested two roots."""
    path.write_text(
        f'mode = "manager"\nschema_version = 1\n{extra}\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n",
        encoding="utf-8",
    )


def _manifest(root: Path, selector: str, version: str) -> None:
    """Create a minimal manifest-backed package version."""
    manifest = root / selector / version / "pkg.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'name = "{selector.rsplit("/", 1)[-1]}"\nversion = "{version[1:].rsplit(".l", 1)[0]}"\nlocalVersion = {version.rsplit(".l", 1)[1]}\n',
        encoding="utf-8",
    )


def test_manager_config_expands_relative_and_case_insensitive_environment_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager roots resolve relative to the file and environment names case-insensitively."""
    config_dir = tmp_path / "manager"
    config_dir.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    config_path = config_dir / "gupkg-config.toml"
    _write_config(config_path, "system", "%userprofile%\\Programs")

    config = load_manager_config(config_path)

    assert config.system_root == (config_dir / "system").resolve()
    assert config.user_root == (tmp_path / "profile" / "Programs").resolve()


def test_manager_config_rejects_unknown_variables_and_nested_roots(tmp_path: Path) -> None:
    """Invalid expansion and overlapping roots fail before inventory discovery."""
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "root", "root/user")
    with pytest.raises(ConfigValidationError, match="non-nested|nested"):
        load_manager_config(config_path)

    _write_config(config_path, "%MISSING_MANAGER_ROOT%", "user")
    with pytest.raises(ConfigValidationError, match="MISSING_MANAGER_ROOT"):
        load_manager_config(config_path)


def test_manager_config_rejects_wrong_mode_schema_and_keys(tmp_path: Path) -> None:
    """Manager configuration accepts only the version-one public schema."""
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")
    config_path.write_text(config_path.read_text().replace('mode = "manager"', 'mode = "other"'))
    with pytest.raises(ConfigValidationError, match="mode"):
        load_manager_config(config_path)
    _write_config(config_path, "system", "user")
    config_path.write_text(config_path.read_text().replace("schema_version = 1", "schema_version = 2"))
    with pytest.raises(ConfigValidationError, match="schema_version"):
        load_manager_config(config_path)
    _write_config(config_path, "system", "user", extra="unexpected = true")
    with pytest.raises(ConfigValidationError, match="unexpected"):
        load_manager_config(config_path)


def test_manager_inventory_keeps_duplicate_selectors_scoped_and_orders_versions(tmp_path: Path) -> None:
    """Duplicate selectors remain distinct and local versions use semantic ordering."""
    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    _manifest(user, "vscode", "v1.9.0.l1")
    _manifest(user, "vscode", "v1.10.0.l1")
    _manifest(system, "vscode", "v2.0.0.l1")
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    inventory = discover_manager(load_manager_config(config_path))

    assert [target.target_id for target in inventory.targets] == ["user:vscode", "system:vscode"]
    assert inventory.targets[0].local_version == "v1.10.0.l1"
    assert all(target.installation_status == "not-installed" for target in inventory.targets)
    with pytest.raises(ValueError, match="ambiguous"):
        select_target(inventory, "vscode")
    assert select_target(inventory, "vscode", Scope.USER).target_id == "user:vscode"


def test_manager_inventory_marks_missing_root_incomplete_without_creating_it(tmp_path: Path) -> None:
    """A missing configured root is reported incomplete and remains absent."""
    system = tmp_path / "system"
    system.mkdir()
    missing_user = tmp_path / "missing-user"
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "missing-user")

    inventory = discover_manager(load_manager_config(config_path))

    user_scope = next(scope for scope in inventory.scopes if scope.scope == Scope.USER)
    assert not user_scope.complete
    assert not missing_user.exists()


def test_manager_inventory_distinguishes_missing_and_broken_current(tmp_path: Path) -> None:
    """A sole local version is not installed and a regular current entry is broken."""
    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    _manifest(user, "not-installed", "v1.0.0.l1")
    _manifest(system, "broken", "v1.0.0.l1")
    (system / "broken" / "current").mkdir()
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    targets = discover_manager(load_manager_config(config_path)).targets

    assert targets[0].installation_status == "broken"
    assert targets[1].installation_status == "not-installed"
