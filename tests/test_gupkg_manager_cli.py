"""Cover manager-mode CLI selection and read-only output.

Real configuration and collection files are used. Package execution and TUI
boundaries are mocked because these tests protect dispatch, not installation.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUPKG_PY = ROOT / "src" / "gupkg" / "gupkg.py"


def _module():
    """Load the public CLI facade used by the console entry point."""
    spec = importlib.util.spec_from_file_location("manager_cli_under_test", GUPKG_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package(root: Path, selector: str = "tool") -> None:
    """Create one manifest-backed package in a collection root."""
    version = root / selector / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        'name = "tool"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )


def _config(directory: Path, system: Path, user: Path) -> None:
    """Write the version-one manager marker for a temporary workspace."""
    (directory / "gupkg-config.toml").write_text(
        'mode = "manager"\nschema_version = 1\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n",
        encoding="utf-8",
    )


def test_implicit_manager_list_is_parseable_and_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An implicit marker selects manager list and emits one TOML document."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    _package(user)
    _config(manager_dir, system, user)
    monkeypatch.chdir(manager_dir)

    output = io.StringIO()
    with redirect_stdout(output):
        code = _module().main(["list", "--scope", "user", "--toml"])

    document = tomllib.loads(output.getvalue())
    assert code == 0
    assert document["manager"]["complete"] is True
    assert document["target"][0]["id"] == "user:tool"


def test_manager_marker_does_not_search_parent_and_broken_marker_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested directories do not inherit a manager marker, including invalid ones."""
    parent = tmp_path / "parent"
    child = parent / "child"
    parent.mkdir()
    child.mkdir()
    _config(parent, tmp_path / "system", tmp_path / "user")
    monkeypatch.chdir(child)
    module = _module()

    with mock.patch.object(module, "_package_main", return_value=17):
        assert module.main(["install", str(child)]) == 17

    (child / "gupkg-config.toml").write_text("mode = 'broken'\n", encoding="utf-8")
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(["list"])
    assert code == 2
    assert "Invalid manager configuration" in output.getvalue()


def test_selected_manager_target_forces_configured_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected user target passes User scope to the existing package parser."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    _package(user)
    _config(manager_dir, system, user)
    monkeypatch.chdir(manager_dir)
    module = _module()

    with mock.patch.object(module, "_package_main", return_value=0) as package_main:
        assert module.main(["--package", "tool", "install"]) == 0

    assert package_main.call_args.args[0][:2] == ["--scope", "User"]
    assert package_main.call_args.args[0][-1] == str(user / "tool")
