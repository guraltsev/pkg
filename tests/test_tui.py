"""Cover package-operation TUI scope labels and command handoff.

The package layout and Textual test driver are real; administrator detection
and subprocess execution are mocked at their operating-system boundaries.
Manager-mode presentation and package operation results are out of scope.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from gupkg.core import Scope
from gupkg.tui import run_tui


def _package_version(root: Path) -> Path:
    """Create a minimal package version that the TUI can inspect."""
    version = root / "ScopeApp" / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        'name = "ScopeApp"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )
    return version


def test_package_tui_uses_system_scope_label_and_cli_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An administrator package install displays System and invokes system scope."""
    version = _package_version(tmp_path)
    captured = []

    from textual.app import App

    monkeypatch.setattr("gupkg.windows.is_current_user_admin", lambda: True)
    monkeypatch.setattr(
        App,
        "run",
        lambda app, *args, **kwargs: captured.append(app),
    )
    monkeypatch.setattr(
        "gupkg.tui.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="completed", stderr=""
        ),
    )

    assert run_tui(str(version)) == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.press("enter")
            options = app.screen.query_one("#command-options")
            rendered = str(options.get_option_at_index(2))
            assert "Installation Scope: System" in rendered
            assert "Machine" not in rendered

            await pilot.press("enter")
            assert "--scope system install" in str(app.screen.query_one("Label").render())

    asyncio.run(drive())


def test_package_tui_keeps_forced_system_scope_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager handoff displays and forwards its forced system scope consistently."""
    version = _package_version(tmp_path)
    captured = []

    from textual.app import App

    monkeypatch.setattr(
        App,
        "run",
        lambda app, *args, **kwargs: captured.append(app),
    )
    monkeypatch.setattr(
        "gupkg.tui.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="completed", stderr=""
        ),
    )

    assert run_tui(str(version), forced_scope=Scope.MACHINE) == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.press("enter")
            options = app.screen.query_one("#command-options")
            assert "Installation Scope: System (locked)" in str(
                options.get_option_at_index(2)
            )
            await pilot.press("enter")
            assert "--scope system install" in str(app.screen.query_one("Label").render())

    asyncio.run(drive())
