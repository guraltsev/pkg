"""Cover registry setup: ``manager init``, optional config keys, and ZIP-based sync.

Manager configuration, the registry cache, archives, and state files are real.
The registry "server" is a ``file:`` URL to a ZIP built in the test, so no
network or Git is involved. Package installation after a registry lookup and
the interactive screens are out of scope.
"""

from __future__ import annotations

import io
import tomllib
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from gupkg import cli
from gupkg.core import ConfigValidationError
from gupkg.manager import load_manager_config
from gupkg.registry import registry_status, search_registry, sync_registry


def _registry_zip(path: Path, *, comment: str = "", wrapper: str = "pkg-stable") -> Path:
    """Write a GitHub-style archive with one valid package seed under ``pkgs``."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{wrapper}/pkgs/tool/v1.0.0/pkg.toml",
            'name = "tool"\nversion = "1.0.0"\nlocalVersion = 0\n',
        )
        archive.writestr(f"{wrapper}/README.md", "ignored")
        archive.comment = comment.encode()
    return path


def _manager_config(directory: Path, registry: str = "") -> Path:
    """Write a manager configuration, optionally with a ``[registry]`` table."""
    path = directory / "gupkg-config.toml"
    path.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{directory / 'system'}'\nuser = '{directory / 'user'}'\n\n"
        f"[bin]\nsystem = '{directory / 'sbin'}'\nuser = '{directory / 'ubin'}'\n" + registry,
        encoding="utf-8",
    )
    return path


def test_registry_table_is_optional_and_cache_and_source_are_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without [registry] the cache is per-user and the source official; both can be set."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    default = load_manager_config(_manager_config(tmp_path))
    assert default.registry_cache == tmp_path / "local" / "gupkg" / "registry"
    assert default.registry_source.startswith("https://github.com/")

    custom = load_manager_config(
        _manager_config(tmp_path, f"\n[registry]\ncache = 'cache'\nsource = 'https://mirror.example/pkgs.zip'\n")
    )
    assert custom.registry_cache == (tmp_path / "cache").resolve()
    assert custom.registry_source == "https://mirror.example/pkgs.zip"


def test_registry_source_must_be_a_url(tmp_path: Path) -> None:
    """A registry source that is not an http(s) or file URL is rejected."""
    with pytest.raises(ConfigValidationError, match="source"):
        load_manager_config(_manager_config(tmp_path, "\n[registry]\nsource = 'git@github.com:o/r.git'\n"))


def test_sync_downloads_archive_and_identifies_revision_by_commit_comment(tmp_path: Path) -> None:
    """Syncing a GitHub-style ZIP publishes its pkgs tree under the commit named in the ZIP comment."""
    commit = "a" * 40
    source = _registry_zip(tmp_path / "registry.zip", comment=commit).as_uri()
    cache = tmp_path / "cache"

    result = sync_registry(cache, source=source)

    assert result.ok and result.status == "synced"
    assert registry_status(cache).revision == commit
    assert [item.selector for item in search_registry(cache, "to")] == ["tool"]
    # The same archive again is recognised as already current.
    assert sync_registry(cache, source=source).status == "current"


def test_sync_failure_keeps_the_previous_registry_usable(tmp_path: Path) -> None:
    """A failed download reports the error and leaves the last validated tree active."""
    cache = tmp_path / "cache"
    good = _registry_zip(tmp_path / "good.zip", comment="b" * 40).as_uri()
    assert sync_registry(cache, source=good).ok

    result = sync_registry(cache, source=(tmp_path / "missing.zip").as_uri())

    assert not result.ok
    assert "previous validated registry tree remains active" in result.warnings[0]
    assert [item.selector for item in search_registry(cache)] == ["tool"]


def test_sync_rejects_archives_that_escape_the_cache(tmp_path: Path) -> None:
    """An archive entry using .. cannot write outside the registry tree."""
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("wrap/pkgs/../../escape.txt", "x")
    cache = tmp_path / "cache"

    result = sync_registry(cache, source=archive_path.as_uri())

    assert not result.ok
    assert not (tmp_path / "escape.txt").exists()
    assert registry_status(cache).tree_path is None


def test_manager_init_creates_a_working_configuration_and_its_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manager init`` writes a loadable configuration, creates its folders, and refuses to overwrite."""
    for name, folder in (("APPDATA", "roaming"), ("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "init"])
    document = tomllib.loads(output.getvalue())

    config = load_manager_config(tmp_path / "roaming" / "gupkg" / "gupkg-config.toml")
    assert code == 0 and document["status"] == "created"
    assert config.user_root.is_dir() and config.user_bin.is_dir()
    assert not tomllib.loads(config.path.read_text(encoding="utf-8")).get("registry", {}).get("channel")

    again = io.StringIO()
    with redirect_stdout(again):
        assert cli.main(["--format", "toml", "manager", "init"]) == 2
    assert "already exists" in tomllib.loads(again.getvalue())["errors"][0]
