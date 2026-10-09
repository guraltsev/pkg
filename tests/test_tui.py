"""Cover package-operation TUI scope labels, operation routing, and status handoff.

The package layout and Textual test driver are real; administrator detection
and the core package operation (``gupkg.commands.run_package_command``) are
mocked at their boundaries, so these tests protect what the user selects and the
exit status they get back. Manager-mode presentation is out of scope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg import commands
from gupkg.core import ActionResult, Scope
from gupkg.outcome import Outcome
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


@pytest.fixture
def operations(monkeypatch: pytest.MonkeyPatch) -> list[commands.PackageRequest]:
    """Record each package request the TUI runs and answer with a failing status 7."""
    requests: list[commands.PackageRequest] = []

    def run(request, *, output=None):
        requests.append(request)
        return Outcome(request.command, ActionResult(False, errors=["failed"], exit_code=7))

    monkeypatch.setattr(commands, "run_package_command", run)
    return requests


def _drive(monkeypatch: pytest.MonkeyPatch, steps) -> None:
    """Replace ``App.run`` with a Textual test-driver session that performs *steps*."""
    from textual.app import App

    def run_app(app, *args, **kwargs):
        async def drive() -> None:
            async with app.run_test(size=(80, 12)) as pilot:
                await steps(app, pilot)
                for _ in range(10):
                    await pilot.pause(0.1)

        asyncio.run(drive())

    monkeypatch.setattr(App, "run", run_app)


def test_administrator_install_shows_system_scope_and_requests_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations
) -> None:
    """An administrator install displays System and runs the install with system scope."""
    version = _package_version(tmp_path)
    monkeypatch.setattr("gupkg.windows.is_current_user_admin", lambda: True)

    async def steps(app, pilot) -> None:
        await pilot.press("enter")
        rendered = str(app.screen.query_one("#command-options").get_option_at_index(2))
        assert "Installation Scope: System" in rendered
        assert "Machine" not in rendered
        await pilot.press("enter")
        assert "--scope system install" in str(app.screen.query_one("Label").render())

    _drive(monkeypatch, steps)
    run_tui(str(version))

    assert [(r.command, r.scope) for r in operations] == [("install", Scope.MACHINE)]


def test_update_selection_runs_a_full_update_and_returns_its_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations
) -> None:
    """The update selection runs the full update and the process status is the operation's."""
    version = _package_version(tmp_path)
    monkeypatch.setattr("gupkg.windows.is_current_user_admin", lambda: False)

    async def steps(app, pilot) -> None:
        # Home order is Install, update check, update download, update.
        await pilot.press("down", "down", "down", "enter")
        await pilot.press("enter")

    _drive(monkeypatch, steps)

    assert run_tui(str(version)) == 7
    [request] = operations
    assert (request.command, request.check_only, request.download_only) == ("update", False, False)
    assert request.scope == Scope.USER
    assert request.path == version


def test_manager_selected_system_scope_stays_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations
) -> None:
    """A manager handoff displays its system scope as locked and runs with it."""
    version = _package_version(tmp_path)

    async def steps(app, pilot) -> None:
        await pilot.press("enter")
        rendered = str(app.screen.query_one("#command-options").get_option_at_index(2))
        assert "Installation Scope: System (locked)" in rendered
        await pilot.press("enter")
        assert "--scope system install" in str(app.screen.query_one("Label").render())

    _drive(monkeypatch, steps)
    run_tui(str(version), forced_scope=Scope.MACHINE)

    assert [(r.command, r.scope) for r in operations] == [("install", Scope.MACHINE)]
