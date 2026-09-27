"""Cover durable manager keyboard navigation through Textual's test driver.

The manager configuration, roots, and inventory are real temporary layouts.
Textual is exercised through its test driver; package providers and package
operations are out of scope because this module covers manager presentation
and handoff behavior.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg import gupkg as cli
from gupkg.manager import discover_manager, load_manager_config
from gupkg.manager_tui import run_manager_tui


def _package(root: Path, selector: str) -> None:
    """Create one manifest-backed package row in a real collection root."""
    version = root / selector / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        f'name = "{selector}"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )


def test_manager_browser_filters_and_handoff_keep_scope_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser reaches duplicate rows, filters them, and displays locked scope on handoff."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    for selector in ("alpha", "beta", "gamma", "delta"):
        _package(user, selector)
    _package(system, "alpha")
    config_path = manager_dir / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 1\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n",
        encoding="utf-8",
    )
    config = load_manager_config(config_path)
    inventory = discover_manager(config)
    captured = []

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    from textual.app import App

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui(config, inventory) == 0
    assert captured
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(42, 8)) as pilot:
            await pilot.press("enter")
            browser = app.screen
            assert "User" in str(browser.query_one("#package-options").get_option_at_index(3))
            await pilot.press("down", "down", "enter")
            details = next(widget for widget in app.screen.query("*") if type(widget).__name__ == "Static")
            assert "Scope: User" in str(details.render())
            assert "locked" in str(details.render())
            await pilot.press("escape")
            assert app.screen is browser

    asyncio.run(drive())


def test_unconfigured_manager_starts_with_one_init_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured manager mode shows a visible header and only initialization."""
    appdata = tmp_path / "AppData" / "Roaming"
    userprofile = tmp_path / "UserProfile"
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "SystemDrive"))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(60, 12)) as pilot:
            assert str(app.query_one("#manager-mode-header").render()) == "MANAGER MODE"
            actions = app.screen.query_one("#manager-init-actions")
            assert len(actions.options) == 1
            assert "Init manager mode" in str(actions.get_option_at_index(0))

    asyncio.run(drive())


def test_manager_tui_discovers_roaming_config_when_handed_off_from_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manager handoff discovers an existing roaming configuration before initialization."""
    appdata = tmp_path / "AppData" / "Roaming"
    manager_dir = appdata / "gupkg"
    manager_dir.mkdir(parents=True)
    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    (manager_dir / "gupkg-config.toml").write_text(
        'mode = "manager"\nschema_version = 1\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    assert captured

    async def drive() -> None:
        app = captured[0]
        async with app.run_test(size=(60, 12)):
            assert app.screen.query_one("#manager-title")

    asyncio.run(drive())


def test_manager_init_proceeds_with_displayed_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-first initialization menu writes a valid config and opens the manager home."""
    appdata = tmp_path / "AppData" / "Roaming"
    userprofile = tmp_path / "UserProfile"
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "SystemDrive"))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    app = captured[0]
    config_path = appdata / "gupkg" / "gupkg-config.toml"

    async def drive() -> None:
        async with app.run_test(size=(80, 18)) as pilot:
            await pilot.press("enter")
            assert app.screen.query_one("#manager-init-options")
            await pilot.press("enter")
            assert app.screen.query_one("#manager-title")
            assert "MANAGER MODE" in str(app.query_one("#manager-mode-header").render())

    asyncio.run(drive())
    assert config_path.is_file()
    config = load_manager_config(config_path)
    assert config.schema_version == 2
    assert config.registry_cache == localappdata / "gupkg" / "registry"


def test_manager_tui_persists_the_selected_shim_linkage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manager home exposes a linkage choice that persists to its TOML file."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    config_path = manager_dir / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 1\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n",
        encoding="utf-8",
    )
    captured = []

    from textual.app import App

    monkeypatch.setattr(App, "run", lambda app, *args, **kwargs: captured.append(app))
    config = load_manager_config(config_path)
    assert run_manager_tui(config, discover_manager(config)) == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(70, 14)) as pilot:
            await pilot.press("down", "down", "down", "enter")
            assert app.screen.query_one("#shim-linkage-options")
            await pilot.press("down", "enter")

    asyncio.run(drive())
    assert load_manager_config(config_path).shim_linkage == "static"


def test_tui_outside_package_directory_opens_manager_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare TUI command selects manager mode when the current directory is not a package."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    selected = []

    def capture_manager(*args, **kwargs):
        selected.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "_run_manager_tui", capture_manager)
    assert cli.main(["tui"]) == 0
    assert selected == [((), {})]
