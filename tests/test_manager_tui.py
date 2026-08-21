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
