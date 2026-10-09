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


_DEFAULT_SUBPROCESS_RUN = subprocess.run


def run_tui(package_path: str = "", *, forced_scope=None) -> int:
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
            "update-check",
            "Update: check for an available update (read-only)",
        ),
        (
            "update-download",
            "Update: download available update (does not install)",
        ),
        (
            "update",
            "Update: check, download, and install available update",
        ),
        ("config-check", "Config: check"),
        ("config-fix", "Config: fix or convert"),
        ("version", "gupkg installer version"),
    )
    flag_labels = (
        ("allow-downgrade", "Allow downgrade"),
        ("refresh-app", "Refresh App from origin"),
        ("no-checksum", "Skip checksum verification"),
        ("allow-hook-dependency-install", "Allow hook dependency installation"),
        ("format-toml", "Render TOML output"),
        ("import-shortcuts", "Import and archive _shortcuts"),
        ("no-backup", "Skip config backup"),
    )

    def action_flags(action: str) -> tuple[tuple[str, str], ...]:
        """Return only the command flags that affect one action."""
        labels = dict(flag_labels)
        flags: tuple[str, ...]
        if action == "install":
            flags = (
                "allow-downgrade",
                "refresh-app",
                "no-checksum",
                "allow-hook-dependency-install",
                "format-toml",
            )
        elif action == "update-check":
            flags = ("allow-hook-dependency-install", "format-toml")
        elif action == "update-download":
            flags = ("no-checksum", "allow-hook-dependency-install", "format-toml")
        elif action == "update":
            flags = ("no-checksum", "allow-hook-dependency-install", "format-toml")
        elif action == "config-check":
            flags = ("format-toml",)
        elif action == "config-fix":
            flags = ("import-shortcuts", "no-backup", "format-toml")
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

            # A bootstrap directory remains a valid selection while a failed
            # promotion leaves another version beside it without ``current``.
            # The summary should retain that selection rather than treating a
            # missing installed-state answer as a missing package.
            try:
                current_identity, _ = resolve_input_path(identity.package_root)
                installed = (
                    current_identity.version_string
                    if current_identity.is_current
                    else "not installed"
                )
            except (OSError, ValueError):
                installed = "not installed"
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

    def scope_label(scope: str) -> str:
        """Return the user-facing label for a CLI scope value."""
        return {"user": "User", "system": "System"}.get(scope, scope)

    def detected_scope(path_text: str) -> tuple[str, bool] | None:
        """Return the automatic scope and System availability for one package."""
        if forced_scope is not None:
            return (forced_scope.value, False)
        from gupkg.layout import resolve_input_path
        from gupkg.windows import is_current_user_admin

        try:
            identity, _ = resolve_input_path(Path(path_text or ".").expanduser())
        except (OSError, ValueError):
            return None
        system_available = is_current_user_admin() and not identity.only_portable_by_name
        return ("system" if system_available else "user"), system_available

    def command_arguments(
        action: str,
        path: str,
        scope: str,
        selected_flags: set[str],
        output: str,
        shim_linkage: str,
    ) -> list[str]:
        """Build the CLI invocation represented by one action list."""
        if action == "version":
            return ["--version"]
        # Root options must precede the subcommand; command-specific options
        # must follow it so the generated argv is valid for argparse.
        args = ["--scope", scope]
        if "allow-hook-dependency-install" in selected_flags:
            args.append("--allow-hook-dependency-install")
        if "format-toml" in selected_flags:
            args.extend(("--format", "toml"))

        if action == "install":
            args.append("install")
            for flag in ("allow-downgrade", "refresh-app", "no-checksum"):
                if flag in selected_flags:
                    args.append(f"--{flag}")
            args.extend(("--shim-linkage", shim_linkage))
        elif action in {"update", "update-check", "update-download"}:
            args.append("update")
            if action == "update-check":
                args.append("--check-only")
            elif action == "update-download":
                args.append("--download-only")
            if "no-checksum" in selected_flags:
                args.append("--no-checksum")
            if action == "update":
                args.extend(("--shim-linkage", shim_linkage))
        elif action == "config-check":
            args.append("config-check")
        elif action == "config-fix":
            args.append("config-fix")
            if "no-backup" in selected_flags:
                args.append("--no-backup")
            enabled = "true" if "import-shortcuts" in selected_flags else "false"
            args.extend(("--import-shortcuts", enabled))
            if output:
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
            options.append(Option("--- Navigation ---", disabled=True))
            if forced_scope is None:
                options.append(Option("Go to manager mode", id="manager-mode"))
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
            elif action == "manager-mode":
                self.app.open_manager = True
                self.app.exit()
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
            scope = detected_scope(home_screen.path)
            self.scope, self.system_available = scope or ("user", False)
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
            if self.action in {
                "install",
                "update",
            }:
                scope = scope_label(self.scope)
                options.append(
                    Option(
                        f"Installation Scope: {scope}{' (locked)' if self.scope_locked else ''}"
                        if self.system_available
                        else f"Installation Scope: {scope} (locked)" if self.scope_locked else "Installation Scope: User (System unavailable)",
                        id="scope",
                        disabled=self.scope_locked or not self.system_available,
                    )
                )
            if self.action in {"install", "update"}:
                options.append(Option(f"Shim linkage: {self.shim_linkage.title()}", id="shim-linkage"))
            if self.action == "config-fix":
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
                            self.shim_linkage,
                        )
                    )
                )
            elif selection == "output":
                self.app.push_screen(PathScreen(self, selection))
            elif selection == "scope":
                if self.system_available:
                    self.scope = "user" if self.scope == "system" else "system"
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
            """Run gupkg and stream human output into the result view."""
            command = [sys.executable, "-m", "gupkg", *self.arguments]
            output = self.query_one("#output", Static)

            # Keep non-terminal test and embedding environments compatible with
            # the ordinary subprocess boundary; an interactive terminal gets
            # line-by-line output so download progress is visible immediately.
            if not sys.stdout.isatty() or subprocess.run is not _DEFAULT_SUBPROCESS_RUN:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                command_output = completed.stdout or "(gupkg produced no output)"
                return_code = completed.returncode
            else:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                )
                lines: list[str] = []
                assert process.stdout is not None
                while True:
                    line = await asyncio.to_thread(process.stdout.readline)
                    if not line:
                        break
                    lines.append(line)
                    output.update("".join(lines))
                return_code = await asyncio.to_thread(process.wait)
                command_output = "".join(lines) or "(gupkg produced no output)"

            output.update(command_output)
            self.app.result_code = return_code
            status = (
                "Completed successfully. Review the result below for the next "
                "step."
                if return_code == 0
                else (
                    f"Failed with exit code {return_code}. Review the "
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
        from gupkg.manager_tui import run_manager_tui

        return max(app.result_code, run_manager_tui())
    return app.result_code
