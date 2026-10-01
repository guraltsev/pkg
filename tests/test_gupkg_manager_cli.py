"""Cover the canonical manager command surface and machine output."""

from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli
from gupkg.core import ActionResult


def _package(root: Path, selector: str = "tool") -> None:
    """Create one manifest-backed package in a collection root."""
    version = root / selector / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        f'name = "{selector}"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )


def _config(directory: Path, system: Path, user: Path) -> Path:
    """Write a complete schema-version-two manager configuration."""
    path = directory / "gupkg-config.toml"
    path.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{system}'\nuser = '{user}'\n\n"
        f"[bin]\nsystem = '{directory / 'system-bin'}'\n"
        f"user = '{directory / 'user-bin'}'\n\n"
        f"[registry]\ncache = '{directory / 'registry'}'\nchannel = 'stable'\n\n"
        "[shims]\nlinkage = 'dynamic'\n",
        encoding="utf-8",
    )
    return path


def test_manager_list_is_scoped_and_parseable(tmp_path: Path) -> None:
    """The explicit manager list command returns target records in TOML mode."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    _package(user)
    config = _config(manager_dir, system, user)

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(
            ["--format", "toml", "--scope", "user", "manager", "--config", str(config), "list"]
        )

    document = tomllib.loads(output.getvalue())
    assert code == 0
    assert document["command"] == "manager.list"
    assert document["target"][0]["id"] == "user:tool"


def test_manager_configuration_is_not_discovered_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cwd marker cannot activate manager mode without an approved location."""
    manager_dir = tmp_path / "manager"
    manager_dir.mkdir()
    _config(manager_dir, tmp_path / "system", tmp_path / "user")
    monkeypatch.chdir(manager_dir)
    monkeypatch.delenv("GUPKG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "list"])

    document = tomllib.loads(output.getvalue())
    assert code == 2
    assert document["ok"] is False
    assert "searched" in document["errors"][0]


def test_manager_tui_owns_format_and_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive manager mode bypasses normal output rendering and pausing."""
    outcome = mock.Mock(result=ActionResult(True, exit_code=7))
    command = mock.Mock(return_value=outcome)
    pause = mock.Mock()
    monkeypatch.setattr(cli, "_manager_command", command)
    monkeypatch.setattr(cli, "wait_for_keypress", pause)

    assert cli.main(["--format", "toml", "--pause", "manager", "tui"]) == 7
    command.assert_called_once()
    pause.assert_not_called()


def test_registry_status_does_not_require_package_roots(tmp_path: Path) -> None:
    """Registry status reports the configured cache even when roots are unavailable."""
    manager_dir = tmp_path / "manager"
    manager_dir.mkdir()
    config = _config(
        manager_dir,
        tmp_path / "missing-system",
        tmp_path / "missing-user",
    )

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(
            [
                "--format",
                "toml",
                "manager",
                "--config",
                str(config),
                "registry",
                "status",
            ]
        )

    document = tomllib.loads(output.getvalue())
    assert code == 0
    assert document["command"] == "manager.registry.status"
    assert document["ok"] is True


def test_manager_max_depth_rejects_nonpositive_values_without_loading_config() -> None:
    """A nonpositive discovery bound is a user error before manager I/O begins."""
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(
            ["--format", "toml", "manager", "--max-depth", "0", "list"]
        )

    document = tomllib.loads(output.getvalue())
    assert code == 2
    assert document["ok"] is False
    assert "max-depth" in document["errors"][0]
