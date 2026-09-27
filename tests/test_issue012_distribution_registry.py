"""Cover manager-v2, custom-payload, and offline-registry behavior.

Manager TOML, package-shaped directories, and registry state are real. Git,
network, Windows elevation, and native launcher execution are out of scope;
these tests protect the validation and cache boundaries that do not require
those external systems.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from gupkg.configuration import normalize_runtime_config
from gupkg.core import ActionResult, ConfigValidationError, ExpansionMode, PackageIdentity, Scope, expand_text, read_toml_file
from gupkg import gupkg as cli
from gupkg.manager import load_manager_config
from gupkg.registry import registry_status, search_registry, validate_registry_tree


def _identity(root: Path, *, selector: str = "tool") -> PackageIdentity:
    """Create a concrete package identity with a valid custom payload tree."""
    version = root / selector / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "gupkg").mkdir()
    (version / "gupkg" / "payload.exe").write_text("payload", encoding="utf-8")
    (version / "pkg.toml").write_text(
        f'name = "{selector}"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )
    return PackageIdentity.from_version_path(root / selector, version, is_current=False)


def test_manager_v2_resolves_independent_roots_bins_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema version two keeps package, command, and registry locations independent."""
    config_path = tmp_path / "gupkg-config.toml"
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    config_path.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        '[packages]\nsystem = "system"\nuser = "%USERPROFILE%/opt"\n\n'
        '[bin]\nsystem = "system-bin"\nuser = "user-bin"\n\n'
        '[registry]\ncache = "cache"\nchannel = "stable"\n',
        encoding="utf-8",
    )

    config = load_manager_config(config_path)

    assert config.system_root == (tmp_path / "system").resolve()
    assert config.user_root == (tmp_path / "profile" / "opt").resolve()
    assert config.system_bin == (tmp_path / "system-bin").resolve()
    assert config.user_bin == (tmp_path / "user-bin").resolve()
    assert config.registry_cache == (tmp_path / "cache").resolve()


def test_version_root_expands_without_rebinding_literal_app(tmp_path: Path) -> None:
    """VersionRoot provides explicit access to sibling directories while App stays literal."""
    identity = _identity(tmp_path)

    result = expand_text(
        "$App|$VersionRoot\\gupkg|$VersionRoot",
        identity,
        ExpansionMode.GENERAL,
    )

    assert result.unresolved == []
    assert result.value.endswith("current\\App|" + str(identity.package_root / "current" / "gupkg") + "|" + str(identity.package_root / "current"))


def test_removed_payload_directory_setting_is_rejected(tmp_path: Path) -> None:
    """The obsolete payload-directory configuration key is not accepted."""
    identity = _identity(tmp_path)

    with pytest.raises(ConfigValidationError, match="Unknown key 'payloadDirectory'"):
        normalize_runtime_config({"payloadDirectory": "gupkg"}, identity)


def test_toml_reader_accepts_utf8_bom(tmp_path: Path) -> None:
    """The TOML reader accepts manifests saved with a UTF-8 BOM."""
    manifest = tmp_path / "pkg.toml"
    manifest.write_bytes('\ufeffname = "tool"\n'.encode("utf-8"))

    assert read_toml_file(manifest)["name"] == "tool"


def test_registry_validation_rejects_multiple_seed_versions(tmp_path: Path) -> None:
    """Registry publication rejects a selector that has more than one seed."""
    pkgs = tmp_path / "pkgs"
    _identity(pkgs, selector="tool")
    second = pkgs / "tool" / "v2.0.0.l1"
    second.mkdir()
    (second / "pkg.toml").write_text('name = "tool"\n', encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="exactly one version seed"):
        validate_registry_tree(pkgs)


def test_offline_registry_search_uses_only_validated_cache(tmp_path: Path) -> None:
    """Offline registry search fails without a validated cache and never creates one."""
    cache = tmp_path / "cache"

    with pytest.raises(FileNotFoundError):
        search_registry(cache, "tool")

    state = registry_status(cache)
    assert state.revision is None
    assert not cache.exists()


def test_manager_bare_install_token_is_resolved_as_registry_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare manager install token selects the offline registry, not a local path."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    cache = tmp_path / "cache"
    for directory in (manager_dir, system, user):
        directory.mkdir()
    _identity(cache / "official" / "trees" / "a" / "pkgs", selector="tool")
    (cache / "official" / "state.toml").parent.mkdir(parents=True, exist_ok=True)
    (cache / "official" / "state.toml").write_text(
        'revision = "a"\nsource = "fixture"\nsynchronized_at = "now"\nlast_failure = ""\n',
        encoding="utf-8",
    )
    (manager_dir / "gupkg-config.toml").write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{system}'\nuser = '{user}'\n\n"
        f"[bin]\nsystem = '{tmp_path / 'system-bin'}'\nuser = '{tmp_path / 'user-bin'}'\n\n"
        f"[registry]\ncache = '{cache}'\nchannel = 'stable'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(manager_dir)

    with mock.patch.object(
        cli, "install_package", return_value=ActionResult(True, exit_code=0)
    ) as install:
        assert cli.main(["install", "tool", "--offline"]) == 0

    assert install.call_args.kwargs["scope"] == Scope.USER
