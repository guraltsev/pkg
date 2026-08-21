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


def test_explicit_config_precedes_cwd_and_each_list_filter_is_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit configuration wins over the cwd marker and list filters expose their rows."""
    cwd = tmp_path / "cwd"
    configured = tmp_path / "configured"
    cwd.mkdir()
    configured.mkdir()
    system = configured / "system"
    user = configured / "user"
    system.mkdir()
    user.mkdir()
    _package(user, "user-tool")
    _package(system, "system-tool")
    _config(configured, system, user)
    _config(cwd, cwd / "wrong-system", cwd / "wrong-user")
    monkeypatch.chdir(cwd)
    module = _module()

    for filter_name, expected_id in (
        ("all", {"user:user-tool", "system:system-tool"}),
        ("installed", set()),
        ("uninstalled", {"user:user-tool", "system:system-tool"}),
        ("unhealthy", set()),
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            code = module.main([
                "--config", str(configured / "gupkg-config.toml"),
                "list", "--filter", filter_name, "--toml",
            ])
        document = tomllib.loads(output.getvalue())
        assert code == 0
        assert {item["id"] for item in document.get("target", [])} == expected_id


def test_doctor_and_dry_run_are_read_only_and_emit_parseable_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor and dry-run inspect real roots without invoking update or mutation boundaries."""
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
    provider_calls = []
    mutation_calls = []

    def unexpected_provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("read-only manager command contacted an update provider")

    monkeypatch.setattr(module, "check_package_update", unexpected_provider)
    monkeypatch.setattr(module, "full_package_upgrade", lambda *args, **kwargs: mutation_calls.append(args))

    doctor_output = io.StringIO()
    with redirect_stdout(doctor_output):
        assert module.main(["doctor", "--toml"]) == 0
    doctor_document = tomllib.loads(doctor_output.getvalue())
    assert doctor_document["manager"]["complete"] is True
    assert provider_calls == []

    dry_run_output = io.StringIO()
    with redirect_stdout(dry_run_output):
        assert module.main(["upgrade", "all", "--dry-run", "--toml"]) == 0
    dry_run_document = tomllib.loads(dry_run_output.getvalue())
    assert dry_run_document["summary"]["total"] == 1
    assert mutation_calls == []
