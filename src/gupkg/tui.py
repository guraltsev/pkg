"""Provide a minimal Textual terminal interface for package operations.

The interface is a plain selectable list: choose an action, then select Run or
one of its settings. It is a thin shell: selections become a
:class:`~gupkg.commands.PackageRequest`, the operation runs in
:mod:`gupkg.commands` on a worker thread with its progress streamed to the
screen, and the outcome is shown with the same formatter the command line uses.

Usage and API
-------------
Run ``gupkg tui`` to start the interface. Call ``run_tui()`` when embedding the
interactive entry point in another Python launcher.

Implementation Approach
-----------------------
Each action uses one borderless option list with Run first and settings below
it. Path editing temporarily replaces that list with one text entry; all other
settings change directly in the list. No workflow, validation, or command-line
construction happens here.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from . import commands
from ._version import __version__
from .core import Scope
from .outcome import Outcome, format_human


def run_tui(package_path: str = "", *, forced_scope: Scope | None = None) -> int:
    """Run the interactive Textual interface.

    Parameters
    ----------
    package_path : str, default=""
        Initially selected package root; an empty value uses the current directory.
    forced_scope : Scope, optional
        Manager-selected scope that is displayed and locked for this package
        operation.  When omitted, package-local automatic scope selection is
        unchanged.

    Returns
    -------
    int
        The exit status of the last operation run from the interface.
    """
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Input, Label, OptionList, Static
    from textual.widgets.option_list import Option

    actions = (
        ("install", "Install"),
        ("update-check", "Update: check for an available update (read-only)"),
        ("update-download", "Update: download available update (does not install)"),
        ("update", "Update: check, download, and install available update"),
        ("config-check", "Config: check"),
        ("config-fix", "Config: fix or convert"),
        ("version", "gupkg installer version"),
    )
    flag_labels = {
        "allow-downgrade": "Allow downgrade",
        "refresh-app": "Refresh App from origin",
        "no-checksum": "Skip checksum verification",
        "allow-hook-dependency-install": "Allow hook dependency installation",
        "import-shortcuts": "Import and archive _shortcuts",
        "no-backup": "Skip config backup",
    }
    # The settings each action offers, in display order.
    action_flags = {
        "install": ("allow-downgrade", "refresh-app", "no-checksum", "allow-hook-dependency-install"),
        "update-check": ("allow-hook-dependency-install",),
        "update-download": ("no-checksum", "allow-hook-dependency-install"),
        "update": ("no-checksum", "allow-hook-dependency-install"),
        "config-fix": ("import-shortcuts", "no-backup"),
    }

    def scope_label(scope: Scope) -> str:
        """Return the user-facing label for a scope."""
        return "System" if scope == Scope.MACHINE else "User"

    def build_request(
        action: str, path: str, scope: Scope, flags: set[str], output: str, shim_linkage: str
    ) -> commands.PackageRequest:
        """Translate the list selections into a package request."""
        command = {"update-check": "update", "update-download": "update"}.get(action, action)
        return commands.PackageRequest(
            command=command,
            path=Path(path) if path else None,
            scope=scope,
            check_only=action == "update-check",
            download_only=action == "update-download",
            allow_downgrade="allow-downgrade" in flags,
            refresh_app="refresh-app" in flags,
            no_checksum="no-checksum" in flags,
            allow_hook_dependency_install="allow-hook-dependency-install" in flags,
            shim_linkage=shim_linkage,
            backup="no-backup" not in flags,
            import_shortcuts="import-shortcuts" in flags,
            output=Path(output) if output else None,
        )

    class HomeScreen(Screen):
        """Present the top-level action list."""

        BINDINGS: ClassVar = [("escape", "exit", "Exit")]

        def __init__(self, initial_path: str) -> None:
            """Resolve the current directory's package summary."""
            super().__init__()
            self.path = initial_path
            self._read_summary()

        def _read_summary(self) -> None:
            """Read the package summary shown above the action list."""
            summary = commands.describe_package(self.path)
            self.title, self.description, self.warning = summary.title, summary.description, summary.warning

        def compose(self) -> ComposeResult:
            """Compose the package summary and action list."""
            yield Label(self.title, id="package-title")
            yield Static(self.description, id="description")
            yield Static(self.warning, id="metadata-warning")
            yield OptionList(*self._options(), id="main-options")

        def _options(self) -> list[Option]:
            """Build the action list followed by global settings."""
            options = [Option(label, id=action) for action, label in actions]
            options.append(Option("--- Settings ---", disabled=True))
            options.append(Option(f"Package path: {self.path or 'current directory'}", id="path"))
            options.append(Option("--- Navigation ---", disabled=True))
            if forced_scope is None:
                options.append(Option("Go to manager mode", id="manager-mode"))
            return options

        def _refresh_summary(self) -> None:
            """Re-read package metadata and refresh the visible summary."""
            self._read_summary()
            self.query_one("#package-title", Label).update(self.title)
            self.query_one("#description", Static).update(self.description)
            self.query_one("#metadata-warning", Static).update(self.warning)

        def on_screen_resume(self) -> None:
            """Refresh package metadata whenever this main screen becomes visible."""
            self._refresh_summary()

        def update_path_setting(self, setting: str, value: str) -> None:
            """Apply the global package-path edit and refresh the main list."""
            _ = setting
            self.path = value
            self._refresh_summary()
            options = self.query_one("#main-options", OptionList)
            options.set_options(self._options())
            options.highlighted = next(i for i, option in enumerate(options.options) if option.id == "path")

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Open the selected action's list."""
            action = event.option.id
            assert isinstance(action, str)
            if action == "path":
                self.app.push_screen(PathScreen(self, "path"))
            elif action == "manager-mode":
                self.app.open_manager = True
                self.app.exit()
            elif action == "version":
                self.app.push_screen(ResultScreen(None, f"gupkg {__version__}"))
            else:
                self.app.push_screen(CommandScreen(action, self))

        def action_exit(self) -> None:
            """Exit directly from the main action list."""
            self.app.exit()

        @on(events.Click, "#description")
        def on_description_clicked(self, event: events.Click) -> None:
            """Open a long package description."""
            if len(self.description) > 80:
                event.stop()
                self.app.push_screen(DescriptionScreen(self.description))

    class CommandScreen(Screen):
        """Present Run and all action settings in one selectable list."""

        BINDINGS: ClassVar = [("escape", "back", "Back")]

        def __init__(self, action: str, home_screen: HomeScreen) -> None:
            """Store the action and its editable settings."""
            super().__init__()
            self.action = action
            self.home_screen = home_screen
            self.output = ""
            self.flags: set[str] = {"import-shortcuts"} if action == "config-fix" else set()
            self.shim_linkage = "dynamic"
            detected = commands.automatic_scope(home_screen.path)
            self.scope, self.system_available = detected or (Scope.USER, False)
            if forced_scope is not None:
                self.scope, self.system_available = forced_scope, False
            self.scope_locked = forced_scope is not None
            self.title = home_screen.title
            self.description = home_screen.description
            self.warning = home_screen.warning

        def compose(self) -> ComposeResult:
            """Compose the summary and one borderless list."""
            yield Label(self.title, id="package-title")
            yield Static(self.description, id="description")
            yield Static(self.warning, id="metadata-warning")
            yield OptionList(*self._options(), id="command-options")

        def on_mount(self) -> None:
            """Make Run the selected default for every action."""
            choices = self.query_one("#command-options", OptionList)
            choices.highlighted = 0
            choices.focus()

        def _options(self) -> list[Option]:
            """Build the one list containing Run and every editable setting."""
            options = [Option("Run", id="run"), Option("--- Settings ---", disabled=True)]
            if self.action in {"install", "update"}:
                scope = scope_label(self.scope)
                if self.scope_locked:
                    text = f"Installation Scope: {scope} (locked)"
                elif self.system_available:
                    text = f"Installation Scope: {scope}"
                else:
                    text = "Installation Scope: User (System unavailable)"
                options.append(
                    Option(text, id="scope", disabled=self.scope_locked or not self.system_available)
                )
                options.append(Option(f"Shim linkage: {self.shim_linkage.title()}", id="shim-linkage"))
            if self.action == "config-fix":
                options.append(Option(f"Output path: {self.output or 'default'}", id="output"))
            options.extend(
                Option(f"{flag_labels[flag]}: {'on' if flag in self.flags else 'off'}", id=flag)
                for flag in action_flags.get(self.action, ())
            )
            return options

        def _refresh_options(self, selected_id: str) -> None:
            """Refresh list values while preserving the changed row's selection."""
            options = self.query_one("#command-options", OptionList)
            options.set_options(self._options())
            options.highlighted = next(i for i, option in enumerate(options.options) if option.id == selected_id)

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Run, edit a path, or toggle the selected setting."""
            selection = event.option.id
            assert isinstance(selection, str)
            if selection == "run":
                request = build_request(
                    self.action, self.home_screen.path, self.scope, self.flags, self.output, self.shim_linkage
                )
                self.app.push_screen(ResultScreen(request))
            elif selection == "output":
                self.app.push_screen(PathScreen(self, selection))
            elif selection == "scope":
                if self.system_available:
                    self.scope = Scope.USER if self.scope == Scope.MACHINE else Scope.MACHINE
                self._refresh_options(selection)
            elif selection == "shim-linkage":
                self.shim_linkage = "static" if self.shim_linkage == "dynamic" else "dynamic"
                self._refresh_options(selection)
            else:
                self.flags.symmetric_difference_update({selection})
                self._refresh_options(selection)

        def update_path_setting(self, setting: str, value: str) -> None:
            """Apply a path edit and refresh package-dependent list rows."""
            _ = setting
            self.output = value
            self._refresh_options(setting)

        def action_back(self) -> None:
            """Return to the action list without execution."""
            self.app.pop_screen()

        @on(events.Click, "#description")
        def on_description_clicked(self, event: events.Click) -> None:
            """Open a long package description."""
            if len(self.description) > 80:
                event.stop()
                self.app.push_screen(DescriptionScreen(self.description))

    class PathScreen(Screen):
        """Edit one path-valued setting without adding form controls to the list."""

        BINDINGS: ClassVar = [("escape", "back", "Back")]

        def __init__(self, owner: Screen, setting: str) -> None:
            """Store the list setting whose text is being edited."""
            super().__init__()
            self.owner = owner
            self.setting = setting

        def compose(self) -> ComposeResult:
            """Compose the one borderless text entry needed for the selected row."""
            value = self.owner.path if self.setting == "path" else self.owner.output
            yield Input(value=value, placeholder="Enter path and press Enter", id="path-editor")

        def on_mount(self) -> None:
            """Focus the path entry immediately."""
            self.query_one(Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            """Save the edited value and return to the action list."""
            self.owner.update_path_setting(self.setting, event.value.strip())
            self.app.pop_screen()

        def action_back(self) -> None:
            """Discard the current edit."""
            self.app.pop_screen()

    class ResultScreen(Screen):
        """Run one package operation and show its live progress and outcome."""

        BINDINGS: ClassVar = [
            ("enter", "main_menu", "Main menu"),
            ("escape", "back", "Back"),
        ]

        def __init__(self, request: commands.PackageRequest | None, text: str = "") -> None:
            """Store the request to run after mounting, or static text to show."""
            super().__init__()
            self.request = request
            self.text = text
            self.lines: list[str] = []

        def compose(self) -> ComposeResult:
            """Compose the command summary and plain output."""
            yield Label(self.request.command_line() if self.request else "gupkg")
            yield Static("Running..." if self.request else "", id="status")
            with VerticalScroll():
                yield Static(self.text, id="output")

        def on_mount(self) -> None:
            """Start the operation after its result view is visible."""
            if self.request is not None:
                self.run_worker(self._run, thread=True, exclusive=True)

        def _run(self) -> None:
            """Run the operation on a worker thread, streaming its progress lines."""
            outcome = commands.run_package_command(
                self.request, output=commands.LineStream(self._progress)
            )
            self.app.call_from_thread(self._finish, outcome)

        def _progress(self, line: str) -> None:
            """Append one progress line to the visible output (from the worker thread)."""
            self.lines.append(line)
            self.app.call_from_thread(self.query_one("#output", Static).update, "\n".join(self.lines))

        def _finish(self, outcome: Outcome) -> None:
            """Show the final outcome and record its exit status."""
            text, problems = format_human(outcome)
            self.query_one("#output", Static).update("\n".join(self.lines + [text + problems]).strip())
            self.app.result_code = outcome.result.exit_code
            self.query_one("#status", Static).update(
                "Completed successfully. Review the result below for the next step."
                if outcome.result.exit_code == 0
                else f"Failed with exit code {outcome.result.exit_code}. Review the output below."
            )

        def action_back(self) -> None:
            """Return to the action list after viewing output."""
            self.app.pop_screen()

        def action_main_menu(self) -> None:
            """Return to the main action list after reviewing command output."""
            while not isinstance(self.app.screen, HomeScreen):
                self.app.pop_screen()

    class DescriptionScreen(Screen):
        """Show a package description that does not fit on the summary line."""

        BINDINGS: ClassVar = [("escape", "back", "Back")]

        def __init__(self, description: str) -> None:
            """Store the complete description for display."""
            super().__init__()
            self.description = description

        def compose(self) -> ComposeResult:
            """Compose the plain, scrollable full-description view."""
            with VerticalScroll():
                yield Static(self.description)

        def action_back(self) -> None:
            """Return to the package summary."""
            self.app.pop_screen()

    class GupkgApp(App):
        """Host the package operation lists."""

        CSS = """
        Screen { padding: 0; }
        Label, Static { margin: 0; }
        OptionList, Input, VerticalScroll {
            background: transparent;
            border: none;
            outline: none;
        }
        OptionList, VerticalScroll { height: 1fr; }
        Input { margin: 0; }
        #description {
            height: 1;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        #metadata-warning { color: $warning; }
        """
        BINDINGS: ClassVar = [("q", "quit", "Quit"), ("b", "back", "Back")]

        def __init__(self, initial_path: str) -> None:
            """Store the package path selected by the dispatcher."""
            super().__init__()
            self.initial_path = initial_path
            self.open_manager = False
            self.result_code = 0

        def on_mount(self) -> None:
            """Start at the action list."""
            self.push_screen(HomeScreen(self.initial_path))

        def action_back(self) -> None:
            """Return one screen when the current screen does not handle Back."""
            if len(self.screen_stack) > 1:
                self.pop_screen()

    app = GupkgApp(package_path)
    app.run()
    if app.open_manager:
        from .manager_tui import run_manager_tui

        return max(app.result_code, run_manager_tui())
    return app.result_code
