"""Provide the interactive manager interface for configured and new users.

The manager app presents the same scoped inventory used by noninteractive
commands, performs update checks in worker threads, and hands one selected
target to the package operation interface. It is a thin shell: every decision,
check, plan, update, and configuration write is a call into
:mod:`gupkg.commands`, so the screens only collect choices and display results.
When no configuration is available, the app remains visibly in manager mode,
offers only initialization, and writes reviewed defaults after explicit
confirmation.

Usage and API
-------------
Call ``run_manager_tui(...)`` with a manager configuration and inventory to
browse targets, open an individual package operation screen, or confirm a
planned batch. Call it without a configuration to present initialization.

Implementation Approach
-----------------------
The home screen summarizes local inventory state, while a single borderless
option list provides filtering and navigation. Detail and progress screens keep
long text and update work separate from the package list; returning from an
operation rebuilds the inventory.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from . import commands
from ._version import __version__
from .core import ConfigValidationError, Scope
from .manager import ManagedTarget, ManagerConfig, ManagerInventory, UpgradePlan, scope_name


def run_manager_tui(
    config: ManagerConfig | None = None,
    inventory: ManagerInventory | None = None,
) -> int:
    """Run the manager browser and open selected targets in package operations.

    Parameters
    ----------
    config : ManagerConfig, optional
        Validated manager configuration whose roots are displayed and scanned.
        When omitted, the standard locations are searched and, if nothing is
        found, the interface opens with an explicit initialization action.
    inventory : ManagerInventory, optional
        Initial inventory, normally supplied by the dispatcher to avoid a
        duplicate scan before the home screen appears.

    Returns
    -------
    int
        Status returned by the selected package operation or the manager app.
    """
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Input, Label, OptionList, Static
    from textual.widgets.option_list import Option

    # Package-mode handoff does not pass manager objects, so look for the
    # standard configuration before presenting the initialization screen.
    if config is None and inventory is None:
        try:
            config = commands.load_manager()
        except ConfigValidationError:
            config = None
    current_inventory = inventory or (commands.discover_manager(config) if config is not None else None)

    def target_label(target: ManagedTarget) -> str:
        """Render all important target dimensions without color dependence."""
        installed = f" {target.installed_version}" if target.installed_version else ""
        installation = target.installation_status.replace("-", " ").title()
        update = target.update_status.replace("-", " ").title()
        if target.candidate_version:
            update += f" {target.candidate_version}"
        return f"{target.package.selector}  {scope_name(target.scope)}  {installation}{installed}  Update {update}"

    def refresh_inventory() -> None:
        """Rebuild the local inventory so screens never show stale versions."""
        nonlocal current_inventory
        current_inventory = commands.discover_manager(config)

    class MissingConfigScreen(Screen):
        """Offer the only safe action when manager mode has no configuration."""

        BINDINGS = [("escape", "quit", "Exit")]

        def compose(self) -> ComposeResult:
            """Compose the manager-mode entry point with one initialization action."""
            yield Label("No manager configuration found", id="manager-init-status")
            yield Static("Manager mode needs a configuration before it can inspect or change packages.")
            yield OptionList(Option("Init manager mode", id="init-manager"), id="manager-init-actions")

        def on_mount(self) -> None:
            """Focus the only available manager action."""
            self.query_one("#manager-init-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Open the reviewed default configuration before writing anything."""
            if event.option.id == "init-manager":
                try:
                    self.app.push_screen(InitManagerScreen())
                except ValueError as exc:
                    self.app.push_screen(TextScreen("Manager initialization unavailable", str(exc)))

        def action_quit(self) -> None:
            """Exit from the unconfigured manager entry point."""
            self.app.exit()

    class InitManagerScreen(Screen):
        """Review, edit, and write the per-user manager configuration."""

        BINDINGS = [("escape", "back", "Back")]

        # (option id, label, ManagerConfig attribute)
        _settings = (
            ("config-file", "Config file", "path"),
            ("system-root", "System packages", "system_root"),
            ("user-root", "User packages", "user_root"),
            ("system-bin", "System executables", "system_bin"),
            ("user-bin", "User executables", "user_bin"),
            ("registry-cache", "Registry cache", "registry_cache"),
            ("registry-source", "Registry source", "registry_source"),
            ("shim-linkage", "Shim linkage", "shim_linkage"),
        )

        def __init__(self) -> None:
            super().__init__()
            self.defaults = commands.default_manager_config()
            self.config = self.defaults

        def compose(self) -> ComposeResult:
            """Compose the default-first initialization menu."""
            yield Label("Initialize manager mode")
            yield Static("Review the values below. Edit any row, reset to defaults, or proceed when ready.")
            yield OptionList(*self._options(), id="manager-init-options")

        def _options(self) -> list[Option]:
            """Build initialization actions and editable settings as text rows."""
            options = [
                Option("Proceed", id="proceed"),
                Option("Reset to defaults", id="reset"),
                Option("--- Settings ---", disabled=True),
            ]
            options.extend(
                Option(f"{label}: {getattr(self.config, attribute) or ''}", id=setting)
                for setting, label, attribute in self._settings
            )
            return options

        def on_mount(self) -> None:
            """Focus the proceed action so Enter accepts the displayed values."""
            self.query_one("#manager-init-options", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Handle proceed, reset, or one editable setting."""
            nonlocal config
            selected = event.option.id
            if selected == "reset":
                self.config = self.defaults
                options = self.query_one("#manager-init-options", OptionList)
                options.set_options(self._options())
                options.highlighted = 1
                return
            if selected != "proceed":
                setting = next((item for item in self._settings if item[0] == selected), None)
                if setting is not None:
                    self.app.push_screen(InitValueScreen(self, setting[2]))
                return
            try:
                config, warnings = commands.init_manager(config=self.config)
                refresh_inventory()
            except (OSError, ValueError, ConfigValidationError) as exc:
                self.app.push_screen(TextScreen("Manager initialization failed", str(exc)))
                return
            self.app.pop_screen()
            self.app.pop_screen()
            self.app.push_screen(HomeScreen())
            if warnings:
                self.app.push_screen(TextScreen("Some folders were not created", "\n".join(warnings)))

        def action_back(self) -> None:
            """Return to the unconfigured manager entry point without writing."""
            self.app.pop_screen()

    class InitValueScreen(Screen):
        """Edit one manager initialization value before it is written."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, init_screen: InitManagerScreen, attribute: str) -> None:
            super().__init__()
            self.init_screen = init_screen
            self.attribute = attribute

        def compose(self) -> ComposeResult:
            """Compose the single text entry for the selected setting."""
            yield Label(f"Edit {self.attribute}")
            yield Input(value=str(getattr(self.init_screen.config, self.attribute) or ""), id="value")

        def on_mount(self) -> None:
            """Focus the setting editor."""
            self.query_one(Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            """Save the edited value into the pending configuration."""
            value: object = event.value.strip()
            if self.attribute not in {"registry_source", "shim_linkage"}:
                value = Path(str(value))
            self.init_screen.config = replace(self.init_screen.config, **{self.attribute: value})
            self.init_screen.query_one("#manager-init-options", OptionList).set_options(
                self.init_screen._options()
            )
            self.app.pop_screen()

        def action_back(self) -> None:
            """Discard the edit and return to initialization settings."""
            self.app.pop_screen()

    class ShimLinkageScreen(Screen):
        """Persist the preferred native shim linkage for later installations."""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self) -> ComposeResult:
            """Compose the two available launcher linkage choices."""
            yield Label("Shim linkage")
            yield Static("Dynamic shims are smaller and install their runtime DLLs and license notices.")
            yield OptionList(
                Option("Dynamic (preferred)", id="dynamic"),
                Option("Static", id="static"),
                id="shim-linkage-options",
            )

        def on_mount(self) -> None:
            """Focus the configured linkage so its current value is visible."""
            options = self.query_one("#shim-linkage-options", OptionList)
            options.highlighted = 0 if config.shim_linkage == "dynamic" else 1
            options.focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Write the selected linkage preference and return to manager home."""
            nonlocal config
            linkage = event.option.id
            if linkage not in {"dynamic", "static"}:
                return
            try:
                config = commands.save_manager_config(replace(config, shim_linkage=linkage))
                refresh_inventory()
            except (OSError, ValueError, ConfigValidationError) as exc:
                self.app.push_screen(TextScreen("Shim linkage update failed", str(exc)))
                return
            self.app.pop_screen()

        def action_back(self) -> None:
            """Return to manager home without changing the configuration."""
            self.app.pop_screen()

    class HomeScreen(Screen):
        """Present manager context and read-only manager actions."""

        BINDINGS = [("escape", "quit", "Exit")]

        def compose(self) -> ComposeResult:
            """Compose the manager summary and action choices."""
            targets = current_inventory.targets
            installed = sum(t.installation_status == "installed" for t in targets)
            unhealthy = sum(t.health_status != "healthy" for t in targets)
            incomplete = [scope for scope in current_inventory.scopes if not scope.complete]
            if incomplete:
                yield Static(
                    "Warning: " + "; ".join(f"{scope_name(s.scope)} root incomplete" for s in incomplete),
                    id="manager-warning",
                )
            yield Label(f"Manager: {config.path}", id="manager-title")
            yield Static(
                f"2 roots  {len(targets)} packages  {installed} installed  {unhealthy} unhealthy",
                id="manager-summary",
            )
            yield OptionList(
                Option("Browse packages", id="browse"),
                Option("Refresh update status", id="refresh"),
                Option("Update all installed packages", id="update"),
                Option(f"Shim linkage: {config.shim_linkage.title()}", id="shim-linkage"),
                Option("Doctor: validate manager and packages", id="doctor"),
                Option("gupkg version", id="version"),
                id="manager-actions",
            )

        def on_mount(self) -> None:
            """Focus the first ordinary choice."""
            self.query_one("#manager-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Open the selected manager action."""
            screens = {
                "browse": BrowserScreen,
                "refresh": RefreshScreen,
                "update": UpgradePlanScreen,
                "shim-linkage": ShimLinkageScreen,
                "doctor": DoctorScreen,
            }
            if event.option.id in screens:
                self.app.push_screen(screens[event.option.id]())
            elif event.option.id == "version":
                self.app.push_screen(TextScreen("gupkg version", f"gupkg {__version__}"))

        def action_quit(self) -> None:
            """Exit from the manager home."""
            self.app.exit()

    class BrowserScreen(Screen):
        """Display a scrollable, keyboard-selectable filtered target list."""

        BINDINGS = [("escape", "back", "Back")]

        _scope_filters = {"All": {Scope.USER, Scope.MACHINE}, "User": {Scope.USER}, "System": {Scope.MACHINE}}
        _status_filters = ("All", "Installed", "Uninstalled", "Updatable", "Unhealthy")

        def __init__(self) -> None:
            super().__init__()
            self.scope_filter = "All"
            self.status_filter = "All"

        def compose(self) -> ComposeResult:
            """Compose filter rows and target rows in one natural list."""
            yield Label("Packages", id="browser-title")
            yield OptionList(*self._options(), id="package-options")

        def _options(self) -> list[Option]:
            """Build the two filter rows followed by the matching packages."""
            visible = commands.filter_targets(
                current_inventory, self._scope_filters[self.scope_filter], self.status_filter
            )
            return [
                Option(f"Scope: {self.scope_filter}", id="scope-filter"),
                Option(f"Filter: {self.status_filter}", id="status-filter"),
                Option("--- Packages ---", disabled=True),
                *(Option(target_label(target), id=target.target_id) for target in visible),
            ]

        def on_mount(self) -> None:
            """Focus the list so it is immediately usable."""
            self.query_one("#package-options", OptionList).focus()

        def _cycle(self, selected_id: str) -> None:
            """Advance one filter to its next value and redraw."""
            if selected_id == "scope-filter":
                values, current = tuple(self._scope_filters), self.scope_filter
            else:
                values, current = self._status_filters, self.status_filter
            value = values[(values.index(current) + 1) % len(values)]
            if selected_id == "scope-filter":
                self.scope_filter = value
            else:
                self.status_filter = value
            options = self.query_one("#package-options", OptionList)
            options.set_options(self._options())
            options.highlighted = 0 if selected_id == "scope-filter" else 1
            options.focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Cycle filters or show details for a selected target."""
            selected = event.option.id
            if selected in {"scope-filter", "status-filter"}:
                self._cycle(selected)
            elif isinstance(selected, str):
                target = next(t for t in current_inventory.targets if t.target_id == selected)
                self.app.push_screen(DetailsScreen(target))

        def action_back(self) -> None:
            """Return to the manager home."""
            self.app.pop_screen()

    class DetailsScreen(Screen):
        """Show complete target context before entering package operations."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, target: ManagedTarget) -> None:
            super().__init__()
            self.target = target

        def compose(self) -> ComposeResult:
            """Compose target identity, paths, status, and actions."""
            target = self.target
            description = ""
            if target.local_version:
                description = commands.describe_package(
                    str(target.package.root / target.local_version)
                ).description
            with VerticalScroll():
                yield Label(target.target_id)
                yield Static(
                    f"Selector: {target.package.selector}\nScope: {scope_name(target.scope)} (locked)\n"
                    f"Path: {target.package.root}\nDescription: {description or '(no description)'}\n"
                    f"Installation: {target.installation_status}\nInstalled version: {target.installed_version or 'none'}\n"
                    f"Local version: {target.local_version or 'none'}\nHealth: {target.health_status}\n"
                    f"Update: {target.update_status}\nDiagnostics: {chr(10).join(target.diagnostics) or 'None'}"
                )
                yield OptionList(Option("Open package operations", id="open"), id="detail-actions")

        def on_mount(self) -> None:
            """Focus the operation handoff."""
            self.query_one("#detail-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Return the selected target to the manager runner."""
            if event.option.id == "open":
                self.app.exit(self.target)

        def action_back(self) -> None:
            """Return to the package browser."""
            self.app.pop_screen()

    class RefreshScreen(Screen):
        """Run update checks in a worker and show progress without blocking UI."""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self) -> ComposeResult:
            """Compose a visible progress view before checks begin."""
            yield Label("Refresh update status")
            yield Static("Checking packages...", id="refresh-status")
            with VerticalScroll():
                yield Static("", id="refresh-output")

        def on_mount(self) -> None:
            """Start checks only after the progress view is visible."""
            self.run_worker(self._refresh(), exclusive=True)

        async def _refresh(self) -> None:
            """Perform provider work off the Textual event loop."""
            results = await asyncio.to_thread(commands.refresh_update_status, current_inventory)
            output = "\n".join(f"{t.target_id}: {t.update_status}" for t in current_inventory.targets)
            self.query_one("#refresh-output", Static).update(output or "No packages discovered.")
            self.query_one("#refresh-status", Static).update(
                "Refresh complete." if all(r.ok for r in results) else "Refresh complete with errors."
            )

        def action_back(self) -> None:
            """Return after reviewing refresh results."""
            self.app.pop_screen()

    class UpgradePlanScreen(Screen):
        """Plan updates without changing package files."""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self) -> ComposeResult:
            """Compose the screen's widgets."""
            yield Label("Update all: plan")
            yield Static("Checking installed packages...", id="plan-status")
            with VerticalScroll(id="plan-output"):
                yield Static("")

        def on_mount(self) -> None:
            # Show the result view before provider work starts, keeping the
            # terminal usable while checks run.
            """Start the screen's work or focus its list once it is visible."""
            self.run_worker(self._plan(), exclusive=True)

        async def _plan(self) -> None:
            plan: UpgradePlan = await asyncio.to_thread(commands.plan_updates, current_inventory, "auto")
            self.query_one("#plan-output Static", Static).update("\n".join(commands.plan_summary_lines(plan)))
            self.query_one("#plan-status", Static).update("Plan complete.")
            self.app.push_screen(ConfirmScreen(plan))

        def action_back(self) -> None:
            """Leave this screen without running anything further."""
            self.app.pop_screen()

    class ConfirmScreen(Screen):
        """Confirm a completed plan and expose all batch safety settings."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, plan: UpgradePlan) -> None:
            super().__init__()
            self.plan = plan
            self.local_deps = False
            self.no_checksum = False

        def _options(self) -> list[Option]:
            return [
                Option("Run planned updates", id="run"),
                Option("Scope: All", id="scope", disabled=True),
                Option(f"Checksum: {'Skip' if self.no_checksum else 'Verify'}", id="checksum"),
                Option(f"Dependency auto-install: {'On' if self.local_deps else 'Off'}", id="deps"),
            ]

        def compose(self) -> ComposeResult:
            """Compose the screen's widgets."""
            yield Label("Confirm update all")
            yield Static("Run planned updates first. Settings apply to this plan.")
            yield OptionList(*self._options(), id="confirm-actions")

        def on_mount(self) -> None:
            """Start the screen's work or focus its list once it is visible."""
            self.query_one("#confirm-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Act on the selected option."""
            selected = event.option.id
            if selected == "run":
                self.app.push_screen(ExecutionScreen(self.plan, self.local_deps, self.no_checksum))
                return
            if selected == "checksum":
                self.no_checksum = not self.no_checksum
            elif selected == "deps":
                self.local_deps = not self.local_deps
            self.query_one("#confirm-actions", OptionList).set_options(self._options())

        def action_back(self) -> None:
            """Leave this screen without running anything further."""
            self.app.pop_screen()

    class ExecutionScreen(Screen):
        """Execute a confirmed plan and retain per-target results."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, plan: UpgradePlan, local_deps: bool, no_checksum: bool) -> None:
            super().__init__()
            self.plan = plan
            self.local_deps = local_deps
            self.no_checksum = no_checksum
            self.cancel_requested = False

        def compose(self) -> ComposeResult:
            """Compose the screen's widgets."""
            yield Label("Update all: execution")
            yield Static("Preparing...", id="execution-status")
            with VerticalScroll(id="execution-output"):
                yield Static("", id="execution-lines")

        def on_mount(self) -> None:
            """Start the screen's work or focus its list once it is visible."""
            self.run_worker(self._execute(), exclusive=True)

        async def _execute(self) -> None:
            lines = self.query_one("#execution-lines", Static)
            status = self.query_one("#execution-status", Static)

            # Elevation is resolved before any mixed-scope mutation; declining
            # it leaves every package unchanged.
            if await asyncio.to_thread(commands.needs_elevation, self.plan):
                accepted = await asyncio.to_thread(
                    commands.relaunch_elevated_update,
                    config,
                    allow_hook_dependency_install=self.local_deps,
                    no_checksum=self.no_checksum,
                )
                refresh_inventory()
                lines.update(
                    "Administrator elevation accepted; the elevated batch was started."
                    if accepted
                    else "Administrator elevation was declined or could not be started; no package was changed."
                )
                status.update("Execution finished.")
                return

            # Run the batch in a worker thread and redraw its per-target
            # states until it finishes, keeping the interface responsive.
            states: dict[str, str] = {}
            execution = asyncio.create_task(asyncio.to_thread(
                commands.execute_updates,
                self.plan,
                config,
                no_checksum=self.no_checksum,
                allow_hook_dependency_install=self.local_deps,
                cancel_requested=lambda: self.cancel_requested,
                on_progress=lambda target_id, state: states.__setitem__(target_id, state),
            ))
            while not execution.done():
                lines.update("\n".join(f"{name}: {state}" for name, state in states.items()) or "Starting...")
                await asyncio.sleep(0.1)
            await execution

            # The browser must observe new state after even a partial batch.
            refresh_inventory()
            report = [f"{name}: {state}" for name, state in states.items()]
            report += [
                f"{entry.target.target_id}: {entry.outcome}"
                for entry in self.plan.entries
                if entry.target.target_id not in states
            ]
            report.append(commands.execution_summary_line(self.plan))
            lines.update("\n".join(report))
            status.update("Execution finished.")

        def action_back(self) -> None:
            # The executor observes this flag between targets; it never
            # interrupts a package operation that has already started.
            """Leave this screen without running anything further."""
            self.cancel_requested = True
            self.query_one("#execution-status", Static).update(
                "Cancellation requested; finishing current target..."
            )

    class DoctorScreen(Screen):
        """Show concise local diagnostics without running provider checks."""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self) -> ComposeResult:
            """Compose diagnostics as a plain scrollable result view."""
            yield Label("Doctor")
            with VerticalScroll():
                yield Static("\n".join(commands.diagnostic_lines(config, current_inventory)))

        def action_back(self) -> None:
            """Return to the manager home."""
            self.app.pop_screen()

    class TextScreen(Screen):
        """Display scrollable plain text for long manager output."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, title: str, text: str) -> None:
            super().__init__()
            self.title_text = title
            self.text = text

        def compose(self) -> ComposeResult:
            """Compose a plain scrollable output view."""
            yield Label(self.title_text)
            with VerticalScroll():
                yield Static(self.text)

        def action_back(self) -> None:
            """Return to the previous manager screen."""
            self.app.pop_screen()

    class ManagerApp(App[ManagedTarget | None]):
        """Host the manager screens with minimal terminal chrome."""

        CSS = """
        Screen { padding: 0; }
        Label, Static { margin: 0; }
        OptionList, VerticalScroll { background: transparent; border: none; outline: none; height: 1fr; }
        #manager-warning { color: $warning; }
        """
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            """Render a persistent header that identifies manager mode."""
            yield Label("MANAGER MODE", id="manager-mode-header")

        def on_mount(self) -> None:
            """Start at the manager home screen."""
            self.push_screen(HomeScreen() if config is not None else MissingConfigScreen())

        def action_quit(self) -> None:
            """Exit from any manager screen."""
            self.exit()

        def action_back(self) -> None:
            """Provide a consistent fallback for Escape and Back."""
            if len(self.screen_stack) > 1:
                self.pop_screen()
            else:
                self.exit()

    from .tui import run_tui

    result_code = 0
    while True:
        selected = ManagerApp().run()
        if selected is None:
            return result_code
        result_code = max(result_code, run_tui(str(selected.package.root), forced_scope=selected.scope))
        # Rebuild the local records before returning to the browser so newly
        # activated versions and repaired health state are immediately visible.
        refresh_inventory()
