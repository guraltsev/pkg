"""Provide a minimal Textual terminal interface for package operations.

The interface is a plain selectable list: choose an action, then select Run or
one of its settings. It delegates execution to the established ``gupkg`` command.

Usage and API
-------------
Run ``gupkg tui`` to start the interface. Call ``run_tui()`` when embedding the
interactive entry point in another Python launcher.

Implementation Approach
-----------------------
Each action uses one borderless option list with Run first and settings below
it. Path editing temporarily replaces that list with one text entry; all other
settings change directly in the list.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import ClassVar


def run_tui(package_path: str = "") -> int:
    """Run the interactive Textual interface.

    Parameters
    ----------
    package_path : str, default=""
        Initially selected package root; an empty value uses the current directory.

    Returns
    -------
    int
        The process status returned after the interface closes.
    """
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Input, Label, OptionList, Static
    from textual.widgets.option_list import Option

    actions = (
        ("install", "Install"),
        (
            "upgrade-check",
            "Upgrade: check for an available update (read-only)",
        ),
        (
            "upgrade-download",
            "Upgrade: download available update (does not install)",
        ),
        (
            "upgrade-full",
            "Upgrade: check, download, and install available update",
        ),
        (
            "upgrade-install",
            "Upgrade: install downloaded update (activates it)",
        ),
        ("config-check", "Config: check"),
        ("config-update", "Config: update"),
        ("config-from-legacy", "Config: import from legacy"),
        ("version", "gupkg installer version"),
    )
    flag_labels = (
        ("use-defaults", "Use defaults if pkg.toml is invalid"),
        ("allow-downgrade", "Allow downgrade"),
        ("refresh-app", "Refresh App from origin"),
        ("no-checksum", "Skip checksum verification"),
        ("dry-run", "Legacy import: dry run"),
        ("toml", "Include TOML status summary"),
        ("local-deps-autoinstall", "Allow pkg.local dependency installation"),
    )

    def action_flags(action: str) -> tuple[tuple[str, str], ...]:
        """Return only the command flags that affect one action."""
        labels = dict(flag_labels)
        flags: tuple[str, ...]
        if action == "install":
            flags = (
                "use-defaults",
                "allow-downgrade",
                "refresh-app",
                "no-checksum",
                "local-deps-autoinstall",
                "toml",
            )
        elif action == "upgrade-check":
            flags = ("local-deps-autoinstall", "toml")
        elif action == "upgrade-download":
            flags = ("no-checksum", "local-deps-autoinstall", "toml")
        elif action == "upgrade-full":
            flags = ("no-checksum", "local-deps-autoinstall", "toml")
        elif action == "upgrade-install" or action in {"config-check", "config-update"}:
            flags = ("toml",)
        elif action == "config-from-legacy":
            flags = ("dry-run",)
        else:
            flags = ()
        return tuple((flag, labels[flag]) for flag in flags)

    def package_summary(path_text: str) -> tuple[str, str, str]:
        """Return package identity, description, and metadata-warning text."""
        from gupkg.configuration import check_metadata_consistency
        from gupkg.core import read_toml_file
        from gupkg.layout import resolve_input_path

        try:
            identity, _ = resolve_input_path(Path(path_text or ".").expanduser())
            config_path = identity.version_path / "pkg.toml"
            config = read_toml_file(config_path) if config_path.exists() else {}
            current_identity, _ = resolve_input_path(identity.package_root)
            installed = (
                current_identity.version_string
                if current_identity.is_current
                else "not installed"
            )
            conflicts = check_metadata_consistency(identity, config)
            warning = (
                "Warning: pkg.toml metadata conflicts with the directory name."
                if conflicts
                else ""
            )
            description = config.get("description", "")
            return (
                f"{identity.name} {identity.version_string}  Installed: {installed}",
                description if isinstance(description, str) else "",
                warning,
            )
        except (OSError, TypeError, ValueError):
            return "No package selected", "Enter a package path to see its summary.", ""

    def detected_scope(path_text: str) -> tuple[str, bool] | None:
        """Return the automatic scope and Machine availability for one package."""
        from gupkg.layout import resolve_input_path
        from gupkg.windows import is_current_user_admin

        try:
            identity, _ = resolve_input_path(Path(path_text or ".").expanduser())
        except (OSError, ValueError):
            return None
        machine_available = is_current_user_admin() and not identity.only_portable_by_name
        return ("Machine" if machine_available else "User"), machine_available

    def command_arguments(
        action: str, path: str, scope: str, selected_flags: set[str], output: str
    ) -> list[str]:
        """Build the CLI invocation represented by one action list."""
        if action == "version":
            return ["--version"]
        args = ["--scope", scope]
        args.extend(f"--{flag}" for flag in selected_flags)
        if action == "install":
            args.append("install")
        else:
            command, operation = action.split("-", maxsplit=1)
            args.extend((command, operation))
        if action == "config-from-legacy" and output:
            args.extend(("--output", output))
        if path:
            args.append(path)
        return args

    class HomeScreen(Screen):
        """Present the top-level action list."""

        BINDINGS: ClassVar = [("escape", "exit", "Exit")]

        def __init__(self, initial_path: str) -> None:
            """Resolve the current directory's package summary."""
            super().__init__()
            self.path = initial_path
            self.title, self.description, self.warning = package_summary(self.path)

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
            return options

        def _refresh_summary(self) -> None:
            """Re-read package metadata and refresh the visible summary."""
            self.title, self.description, self.warning = package_summary(self.path)
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
            options.highlighted = next(
                index for index, option in enumerate(options.options) if option.id == "path"
            )

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            """Open the selected action's list."""
            action = event.option.id
            assert isinstance(action, str)
            if action == "path":
                self.app.push_screen(PathScreen(self, "path"))
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
            self.flags: set[str] = set()
            scope = detected_scope(home_screen.path)
            self.scope, self.machine_available = scope or ("User", False)
            self.title = home_screen.title
            self.description = home_screen.description
            self.warning = home_screen.warning

        def compose(self) -> ComposeResult:
            """Compose the summary and one borderless list."""
            yield Label(self.title, id="package-title")
            yield Static(self.description, id="description")
            yield Static(self.warning, id="metadata-warning")
            yield OptionList(*self._options(), id="command-options")

        def __init__(self, initial_path: str) -> None:
            """Store the package path selected by the dispatcher."""
            super().__init__()
            self.initial_path = initial_path

        def on_mount(self) -> None:
            """Make Run the selected default for every action."""
            choices = self.query_one("#command-options", OptionList)
            choices.highlighted = 0
            choices.focus()

        def _options(self) -> list[Option]:
            """Build the one list containing Run and every editable setting."""
            options = [Option("Run", id="run"), Option("--- Settings ---", disabled=True)]
            if self.action in {
                "install",
                "upgrade-install",
                "upgrade-full",
            }:
                scope = "Machine" if self.scope == "Machine" else "Local"
                options.append(
                    Option(
                        f"Installation Scope: {scope}"
                        if self.machine_available
                        else "Installation Scope: Local (Machine unavailable)",
                        id="scope",
                        disabled=not self.machine_available,
                    )
                )
            if self.action == "config-from-legacy":
                options.append(Option(f"Output path: {self.output or 'default'}", id="output"))
            options.extend(
                Option(f"{label}: {'on' if flag in self.flags else 'off'}", id=flag)
                for flag, label in action_flags(self.action)
            )
            return options

        def _refresh_options(self, selected_id: str) -> None:
            """Refresh list values while preserving the changed row's selection."""
            options = self.query_one("#command-options", OptionList)
            options.set_options(self._options())
            options.highlighted = next(
                index for index, option in enumerate(options.options) if option.id == selected_id
            )

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            """Run, edit a path, or toggle the selected setting."""
            selection = event.option.id
            assert isinstance(selection, str)
            if selection == "run":
                self.app.push_screen(
                    ResultScreen(
                        command_arguments(
                            self.action,
                            self.home_screen.path,
                            self.scope,
                            self.flags,
                            self.output,
                        )
                    )
                )
            elif selection == "output":
                self.app.push_screen(PathScreen(self, selection))
            elif selection == "scope":
                if self.machine_available:
                    self.scope = "User" if self.scope == "Machine" else "Machine"
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

        def __init__(self, command_screen: CommandScreen, setting: str) -> None:
            """Store the list setting whose text is being edited."""
            super().__init__()
            self.command_screen = command_screen
            self.setting = setting

        def compose(self) -> ComposeResult:
            """Compose the one borderless text entry needed for the selected row."""
            value = (
                self.command_screen.path
                if self.setting == "path"
                else self.command_screen.output
            )
            yield Input(value=value, placeholder="Enter path and press Enter", id="path-editor")

        def on_mount(self) -> None:
            """Focus the path entry immediately."""
            self.query_one(Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            """Save the edited value and return to the action list."""
            self.command_screen.update_path_setting(self.setting, event.value.strip())
            self.app.pop_screen()

        def action_back(self) -> None:
            """Discard the current edit."""
            self.app.pop_screen()

    class ResultScreen(Screen):
        """Run one gupkg command and show its output in a scrollable view."""

        BINDINGS: ClassVar = [
            ("enter", "main_menu", "Main menu"),
            ("escape", "back", "Back"),
        ]

        def __init__(self, arguments: list[str]) -> None:
            """Store the command arguments to execute after mounting."""
            super().__init__()
            self.arguments = arguments

        def compose(self) -> ComposeResult:
            """Compose the command summary and plain output."""
            yield Label("gupkg " + " ".join(self.arguments))
            yield Static("Running...", id="status")
            with VerticalScroll():
                yield Static("", id="output")

        def on_mount(self) -> None:
            """Start the command after its result view is visible."""
            self.run_worker(self._run_command(), exclusive=True)

        async def _run_command(self) -> None:
            """Run gupkg without corrupting Textual's terminal rendering."""
            command = [sys.executable, "-m", "gupkg", *self.arguments]
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.query_one("#output", Static).update(
                completed.stdout or "(gupkg produced no output)"
            )
            status = (
                "Completed successfully. Review the result below for the next "
                "step."
                if completed.returncode == 0
                else (
                    f"Failed with exit code {completed.returncode}. Review the "
                    "output below."
                )
            )
            self.query_one("#status", Static).update(status)

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

        def on_mount(self) -> None:
            """Start at the action list."""
            self.push_screen(HomeScreen(self.initial_path))

        def action_back(self) -> None:
            """Return one screen when the current screen does not handle Back."""
            if len(self.screen_stack) > 1:
                self.pop_screen()

    GupkgApp(package_path).run()
    return 0
