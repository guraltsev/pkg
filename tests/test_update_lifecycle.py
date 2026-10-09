"""Cover the check → download → install lifecycle and its user-visible reporting.

Package trees, receipts, update state, wrapper files, and NTFS junctions are
real. The GitHub/HTTP boundary (``urllib.request.urlopen``) and the Windows
registry accessors used for PATH are mocked so no network or host PATH is
touched. Shortcut creation, elevation, and the TUIs are out of scope.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli, components
from gupkg import gupkg as workflows
from gupkg.core import Scope
from gupkg.windows import create_junction


def _archive(name: str, content: str) -> bytes:
    """Return a ZIP archive containing one application file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


def _release(version: str, archive: bytes, release_id: int = 7) -> bytes:
    """Return a GitHub latest-release document with one verified asset."""
    return json.dumps({
        "id": release_id,
        "tag_name": f"v{version}",
        "assets": [{
            "name": "tool.zip",
            "state": "uploaded",
            "browser_download_url": "https://example.invalid/tool.zip",
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        }],
    }).encode()


def _github_package(root: Path, name: str = "Tool", extra: str = "") -> Path:
    """Create an installed-looking ``v1.0.0`` package that updates from GitHub releases."""
    version = root / name / "v1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("old", encoding="utf-8")
    (version / "pkg.toml").write_text(
        f'name = "{name}"\nversion = "1.0.0"\nlocalVersion = 0\n{extra}\n'
        '[origin]\nurl = "https://github.com/owner/tool"\n\n'
        '[update.check]\nmode = "github"\nassetName = "tool.zip"\n\n'
        '[update.payload]\nmode = "zip"\n',
        encoding="utf-8",
    )
    return version


@pytest.fixture
def user_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user-scope Start Menu and profile at a temporary home."""
    home = tmp_path / "home"
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def fake_path_registry(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    """Replace PATH registry access with an initially empty in-memory value."""
    write = mock.Mock()
    monkeypatch.setattr(components, "read_registry_value", mock.Mock(side_effect=FileNotFoundError))
    monkeypatch.setattr(components, "write_registry_value", write)
    monkeypatch.setattr(components, "broadcast_environment_change", mock.Mock())
    return write


def test_full_update_activates_a_version_staged_by_an_earlier_download(tmp_path: Path, user_env: Path) -> None:
    """``update`` after ``update --download-only`` installs the staged version instead of failing.

    The second check rediscovers the same candidate; the already committed
    version and its pending receipt are reused, and activation consumes the
    receipt.
    """
    version = _github_package(tmp_path)
    archive = _archive("tool.exe", "new")
    responses = [io.BytesIO(_release("2.0.0", archive)), io.BytesIO(archive), io.BytesIO(_release("2.0.0", archive))]

    with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
        downloaded = workflows.download_package_update(version)
        installed = workflows.full_package_upgrade(version, scope=Scope.USER)

    package_root = version.parent
    assert downloaded.status == "downloaded" and downloaded.changed
    assert installed.ok, installed.errors
    assert installed.status == "installed-update"
    assert (package_root / "current" / "App" / "tool.exe").read_text(encoding="utf-8") == "new"
    assert list((package_root / ".gupkg" / "receipts").iterdir()) == []


def test_full_update_refuses_a_different_release_for_an_already_staged_version(
    tmp_path: Path, user_env: Path
) -> None:
    """A republished release never activates the payload staged from the earlier one."""
    version = _github_package(tmp_path)
    archive = _archive("tool.exe", "first")
    responses = [io.BytesIO(_release("2.0.0", archive)), io.BytesIO(archive), io.BytesIO(_release("2.0.0", archive, release_id=8))]

    with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
        workflows.download_package_update(version)
        result = workflows.full_package_upgrade(version, scope=Scope.USER)

    assert not result.ok
    assert "already exists" in result.errors[0]
    assert not (version.parent / "current").exists()


def test_human_output_reports_each_error_exactly_once(tmp_path: Path) -> None:
    """A failed command prints every error once, after its status line."""
    version = tmp_path / "Tool" / "v1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "pkg.toml").write_text('name = "Other"\nversion = "1.0.0"\n', encoding="utf-8")

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = cli.main(["config-check", str(version)])

    assert code == 2
    assert output.getvalue().count("Name mismatch: directory='Tool', config='Other'") == 1
    assert "config-check: failed" in output.getvalue()


def test_install_never_rewrites_a_path_that_cannot_be_read(
    tmp_path: Path, user_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable registry PATH fails the install without replacing PATH."""
    version = tmp_path / "Tool" / "v1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("x", encoding="utf-8")
    (version / "pkg.toml").write_text(
        'name = "Tool"\nversion = "1.0.0"\n\n[[path]]\nvalue = "$App"\n', encoding="utf-8"
    )
    write = mock.Mock()
    monkeypatch.setattr(components, "read_registry_value", mock.Mock(side_effect=PermissionError("denied")))
    monkeypatch.setattr(components, "write_registry_value", write)

    result = workflows.install_package(version, scope=Scope.USER)

    assert not result.ok
    assert any("PATH" in error for error in result.errors)
    write.assert_not_called()


def _manager(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a manager configuration and return it with its user root and user bin."""
    user_root, user_bin = tmp_path / "user", tmp_path / "user-bin"
    (tmp_path / "system").mkdir()
    user_root.mkdir()
    config = tmp_path / "gupkg-config.toml"
    config.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{tmp_path / 'system'}'\nuser = '{user_root}'\n\n"
        f"[bin]\nsystem = '{tmp_path / 'system-bin'}'\nuser = '{user_bin}'\n\n"
        f"[registry]\ncache = '{tmp_path / 'cache'}'\nchannel = 'stable'\n",
        encoding="utf-8",
    )
    return config, user_root, user_bin


def _toml_cli(*arguments: str) -> tuple[int, dict]:
    """Run the CLI in TOML mode and parse its single result document."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(["--format", "toml", *arguments])
    return code, tomllib.loads(output.getvalue())


def test_manager_check_reports_the_available_candidate_version(tmp_path: Path) -> None:
    """``manager update --check-only`` names the version each updatable target would get."""
    config, user_root, _ = _manager(tmp_path)
    version = _github_package(user_root)
    create_junction(version.parent / "current", version)
    archive = _archive("tool.exe", "new")

    with mock.patch.object(urllib.request, "urlopen", return_value=io.BytesIO(_release("2.0.0", archive))):
        code, document = _toml_cli("manager", "--config", str(config), "update", "--check-only")

    assert code == 0, document["errors"]
    assert document["target"][0]["status"] == "available"
    assert document["target"][0]["candidate_version"] == "2.0.0"


def test_manager_update_installs_wrappers_into_the_configured_bin_directory(
    tmp_path: Path, user_env: Path, fake_path_registry: mock.Mock
) -> None:
    """A manager update activates the new version with the manager's ``[bin]`` destination."""
    config, user_root, user_bin = _manager(tmp_path)
    version = _github_package(user_root, extra='\n[[bin]]\nname = "tool.cmd"\ncontent = "@echo off"\n')
    create_junction(version.parent / "current", version)
    archive = _archive("tool.exe", "new")
    responses = [io.BytesIO(_release("2.0.0", archive)), io.BytesIO(_release("2.0.0", archive)), io.BytesIO(archive)]

    with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
        code, document = _toml_cli("manager", "--config", str(config), "update", "--yes")

    assert code == 0, document
    assert document["target"][0]["status"] == "installed-update"
    assert (user_bin / "tool.cmd").read_text(encoding="utf-8") == "@echo off"
    assert not (user_env / "bin" / "tool.cmd").exists()
