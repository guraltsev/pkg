"""Provide the interactive, read-first interface for a fixed manager directory.

The manager app presents the same scoped inventory used by noninteractive
commands, performs update checks in worker threads, and hands one selected
target to the established package operation interface.  Aggregate upgrades
use the manager planner and executor, including their safety and result rules.

Usage and API
-------------
Call ``run_manager_tui(...)`` with a manager inventory to browse targets,
open an individual package operation screen, or confirm a planned batch.

Implementation Approach
-----------------------
The home screen summarizes local inventory state, while a single borderless
option list provides filtering and navigation.  Detail and progress screens
keep long text and update work separate from the package list; returning from
an operation rebuilds the inventory through the supplied loader.
"""

from __future__ import annotations

import asyncio
import contextlib
import io

from .manager import (
    ManagerConfig,
    ManagerInventory,
    ManagedTarget,
    UpgradePlan,
    discover_manager,
    execute_upgrade_plan,
    plan_upgrade_all,
    scope_name,
)
from .core import Scope


def run_manager_tui(config: ManagerConfig, inventory: ManagerInventory | None = None) -> int:
    """Run the manager browser and open selected targets in package operations.

    Parameters
    ----------
    config : ManagerConfig
        Validated manager configuration whose roots are displayed and scanned.
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
    from textual.widgets import Label, OptionList, Static
    from textual.widgets.option_list import Option

    current_inventory = inventory or discover_manager(config)

    def target_label(target: ManagedTarget) -> str:
        """Render all important target dimensions without color dependence."""
        installed = target.installed_version or ""
        installation = target.installation_status.replace("-", " ").title()
        version = f" {installed}" if installed else ""
        update = target.update_status.replace("-", " ").title()
        if target.candidate_version:
            update += f" {target.candidate_version}"
        return f"{target.package.selector}  {scope_name(target.scope)}  {installation}{version}  Update {update}"

    def matches(target: ManagedTarget, scope_filter: str, status_filter: str) -> bool:
        """Apply the visible scope and installation-health filters."""
        if scope_filter != "All" and scope_name(target.scope) != scope_filter:
            return False
        return {
            "All": True,
            "Installed": target.installation_status == "installed",
            "Uninstalled": target.installation_status == "not-installed",
            "Updatable": target.update_status == "available",
            "Unhealthy": target.health_status != "healthy",
        }[status_filter]

    class HomeScreen(Screen):
        """Present manager context and read-only manager actions."""

        BINDINGS = [("escape", "quit", "Exit")]

        def compose(self) -> ComposeResult:
            """Compose the manager summary and action choices."""
            installed = sum(t.installation_status == "installed" for t in current_inventory.targets)
            unhealthy = sum(t.health_status != "healthy" for t in current_inventory.targets)
            incomplete = [scope for scope in current_inventory.scopes if not scope.complete]
            if incomplete:
                warning = "Warning: " + "; ".join(
                    f"{scope_name(scope.scope)} root incomplete" for scope in incomplete
                )
                yield Static(warning, id="manager-warning")
            yield Label(f"Manager: {config.path}", id="manager-title")
            yield Static(
                f"2 roots  {len(current_inventory.targets)} packages  {installed} installed  {unhealthy} unhealthy",
                id="manager-summary",
            )
            yield OptionList(
                Option("Browse packages", id="browse"),
                Option("Refresh update status", id="refresh"),
                Option("Upgrade all installed packages", id="upgrade"),
                Option("Doctor: validate manager and packages", id="doctor"),
                Option("gupkg version", id="version"),
                id="manager-actions",
            )

        def on_mount(self) -> None:
            """Focus the first ordinary choice."""
            self.query_one("#manager-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Open the selected manager action."""
            action = event.option.id
            if action == "browse":
                self.app.push_screen(BrowserScreen())
            elif action == "refresh":
                self.app.push_screen(RefreshScreen())
            elif action == "upgrade":
                self.app.push_screen(UpgradePlanScreen())
            elif action == "doctor":
                self.app.push_screen(DoctorScreen())
            elif action == "version":
                self.app.push_screen(TextScreen("gupkg version", "gupkg manager interface"))

        def action_quit(self) -> None:
            """Exit from the manager home."""
            self.app.exit()

    class BrowserScreen(Screen):
        """Display a scrollable, keyboard-selectable filtered target list."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self) -> None:
            super().__init__()
            self.scope_filter = "All"
            self.status_filter = "All"

        def compose(self) -> ComposeResult:
            """Compose filter rows and target rows in one natural list."""
            yield Label("Packages", id="browser-title")
            yield OptionList(*self._options(), id="package-options")

        def _visible_targets(self) -> list[ManagedTarget]:
            return [
                target for target in current_inventory.targets
                if matches(target, self.scope_filter, self.status_filter)
            ]

        def _options(self) -> list[Option]:
            options = [
                Option(f"Scope: {self.scope_filter}", id="scope-filter"),
                Option(f"Filter: {self.status_filter}", id="status-filter"),
                Option("--- Packages ---", disabled=True),
            ]
            options.extend(Option(target_label(target), id=target.target_id) for target in self._visible_targets())
            return options

        def on_mount(self) -> None:
            """Focus the scope filter so the list is immediately usable."""
            self.query_one("#package-options", OptionList).focus()

        def _cycle(self, selected_id: str) -> None:
            values = ("All", "User", "System") if selected_id == "scope-filter" else (
                "All", "Installed", "Uninstalled", "Updatable", "Unhealthy"
            )
            current = self.scope_filter if selected_id == "scope-filter" else self.status_filter
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
                return
            if isinstance(selected, str) and selected not in {None, "--- Packages ---"}:
                target = next(target for target in current_inventory.targets if target.target_id == selected)
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
            description = "(no description)"
            if target.local_version:
                from .core import read_toml_file

                manifest = next(
                    (item for item in target.package.manifests if item.version_path.name == target.local_version),
                    None,
                )
                if manifest is not None:
                    value = read_toml_file(manifest.path).get("description", "")
                    if isinstance(value, str) and value:
                        description = value
            diagnostics = "\n".join(target.diagnostics) or "None"
            with VerticalScroll():
                yield Label(target.target_id)
                yield Static(
                    f"Selector: {target.package.selector}\nScope: {scope_name(target.scope)} (locked)\n"
                    f"Path: {target.package.root}\nDescription: {description}\n"
                    f"Installation: {target.installation_status}\nInstalled version: {target.installed_version or 'none'}\n"
                    f"Local version: {target.local_version or 'none'}\nHealth: {target.health_status}\n"
                    f"Update: {target.update_status}\nDiagnostics: {diagnostics}"
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
            from gupkg.gupkg import _manager_update

            targets = list(current_inventory.targets)
            results = await asyncio.gather(*(asyncio.to_thread(_manager_update, target) for target in targets))
            output = "\n".join(f"{target.target_id}: {target.update_status}" for target in targets)
            self.query_one("#refresh-output", Static).update(output or "No packages discovered.")
            self.query_one("#refresh-status", Static).update(
                "Refresh complete." if all(result.ok for result in results) else "Refresh complete with errors."
            )

        def action_back(self) -> None:
            """Return after reviewing refresh results."""
            self.app.pop_screen()

    class UpgradePlanScreen(Screen):
        """Plan updates without changing package files."""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self) -> ComposeResult:
            yield Label("Upgrade all: plan")
            yield Static("Checking installed packages...", id="plan-status")
            with VerticalScroll(id="plan-output"):
                yield Static("")

        def on_mount(self) -> None:
            # Show the result view before provider work starts, keeping the
            # terminal usable while checks run.
            self.run_worker(self._plan(), exclusive=True)

        async def _plan(self) -> None:
            from gupkg.gupkg import _manager_update

            self.plan: UpgradePlan = await asyncio.to_thread(
                plan_upgrade_all,
                current_inventory,
                {Scope.USER, Scope.MACHINE},
                _manager_update,
            )
            available = sum(entry.outcome == "eligible" for entry in self.plan.entries)
            current = sum(entry.reason == "current" for entry in self.plan.entries)
            skipped = sum(entry.outcome == "skipped" and entry.reason != "current" for entry in self.plan.entries)
            failed = sum(entry.outcome == "failed" for entry in self.plan.entries)
            lines = [
                f"Available: {available}",
                f"Current: {current}",
                f"Skipped: {skipped}",
                f"Failed checks: {failed}",
                "",
            ]
            lines.extend(
                f"{entry.target.target_id}: {entry.outcome}"
                + (f" ({entry.reason})" if entry.reason else "")
                for entry in self.plan.entries
            )
            self.query_one("#plan-output Static", Static).update("\n".join(lines))
            self.query_one("#plan-status", Static).update("Plan complete.")
            self.app.push_screen(ConfirmScreen(self.plan))

        def action_back(self) -> None:
            self.app.pop_screen()

    class ConfirmScreen(Screen):
        """Confirm a completed plan and expose all batch safety settings."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, plan: UpgradePlan) -> None:
            super().__init__()
            self.plan = plan
            self.fail_fast = False
            self.local_deps = False
            self.no_checksum = False

        def compose(self) -> ComposeResult:
            yield Label("Confirm upgrade all")
            yield Static("Run planned upgrades first. Settings apply to this plan.")
            yield OptionList(
                Option("Run planned upgrades", id="run"),
                Option("Scope: All", id="scope", disabled=True),
                Option("Checksum: Verify", id="checksum"),
                Option("Dependency auto-install: Off", id="deps"),
                Option("Fail-fast: Off", id="fail-fast"),
                id="confirm-actions",
            )

        def _refresh_options(self) -> None:
            self.query_one("#confirm-actions", OptionList).set_options([
                Option("Run planned upgrades", id="run"),
                Option("Scope: All", id="scope", disabled=True),
                Option(f"Checksum: {'Skip' if self.no_checksum else 'Verify'}", id="checksum"),
                Option(f"Dependency auto-install: {'On' if self.local_deps else 'Off'}", id="deps"),
                Option(f"Fail-fast: {'On' if self.fail_fast else 'Off'}", id="fail-fast"),
            ])

        def on_mount(self) -> None:
            self.query_one("#confirm-actions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            selected = event.option.id
            if selected == "run":
                self.app.push_screen(ExecutionScreen(self.plan, self.fail_fast, self.local_deps, self.no_checksum))
            elif selected == "checksum":
                self.no_checksum = not self.no_checksum
                self._refresh_options()
            elif selected == "deps":
                self.local_deps = not self.local_deps
                self._refresh_options()
            elif selected == "fail-fast":
                self.fail_fast = not self.fail_fast
                self._refresh_options()

        def action_back(self) -> None:
            self.app.pop_screen()

    class ExecutionScreen(Screen):
        """Execute a confirmed plan and retain per-target results."""

        BINDINGS = [("escape", "back", "Back")]

        def __init__(self, plan: UpgradePlan, fail_fast: bool, local_deps: bool, no_checksum: bool) -> None:
            super().__init__()
            self.plan = plan
            self.fail_fast = fail_fast
            self.local_deps = local_deps
            self.no_checksum = no_checksum
            self.cancel_requested = False

        def compose(self) -> ComposeResult:
            yield Label("Upgrade all: execution")
            yield Static("Preparing...", id="execution-status")
            with VerticalScroll(id="execution-output"):
                yield Static("", id="execution-lines")

        def on_mount(self) -> None:
            self.run_worker(self._execute(), exclusive=True)

        async def _execute(self) -> None:
            nonlocal current_inventory
            from gupkg.gupkg import (
                _manager_revalidate,
                full_package_upgrade,
            )
            from gupkg.windows import is_current_user_admin, relaunch_elevated

            eligible = [entry for entry in self.plan.entries if entry.outcome == "eligible"]
            needs_elevation = any(entry.target.scope == Scope.MACHINE for entry in eligible)
            lines: list[str] = []
            if needs_elevation and not is_current_user_admin():
                relaunch_args = ["--config", str(config.path), "upgrade", "all", "--yes"]
                if self.fail_fast:
                    relaunch_args.append("--fail-fast")
                if self.local_deps:
                    relaunch_args.append("--local-deps-autoinstall")
                if self.no_checksum:
                    relaunch_args.append("--no-checksum")
                accepted = await asyncio.to_thread(relaunch_elevated, relaunch_args)
                message = (
                    "Administrator elevation accepted; the elevated batch was started."
                    if accepted else
                    "Administrator elevation was declined or could not be started; no package was changed."
                )
                # Refresh even when elevation is declined or delegated so the
                # browser never presents the pre-execution inventory forever.
                current_inventory = await asyncio.to_thread(discover_manager, config)
                self.query_one("#execution-lines", Static).update(message)
                self.query_one("#execution-status", Static).update("Execution finished.")
                return

            def revalidate(target: ManagedTarget) -> str | None:
                root = config.system_root if target.scope == Scope.MACHINE else config.user_root
                return _manager_revalidate(target, root, quiet=True)

            def upgrade(target: ManagedTarget):
                lines.append(f"{target.target_id}: running")
                with contextlib.redirect_stdout(io.StringIO()):
                    result = full_package_upgrade(
                        target.package.root,
                        scope=target.scope,
                        no_checksum=self.no_checksum,
                        local_deps_autoinstall=self.local_deps,
                    )
                lines[-1] = f"{target.target_id}: {'completed' if result.ok else 'failed'}"
                return result

            await asyncio.to_thread(
                execute_upgrade_plan,
                self.plan,
                revalidate,
                upgrade,
                fail_fast=self.fail_fast,
                cancel_requested=lambda: self.cancel_requested,
            )
            # The browser must observe new current/version state after even a
            # partial batch, so a later return never relies on stale targets.
            current_inventory = await asyncio.to_thread(discover_manager, config)
            summary = self.plan.entries
            lines.extend(
                f"{entry.target.target_id}: {entry.outcome}"
                for entry in summary
                if not any(entry.target.target_id in line for line in lines)
            )
            lines.append(
                "Summary: "
                f"{sum(entry.outcome == 'upgraded' for entry in summary)} upgraded, "
                f"{sum(entry.outcome == 'failed' for entry in summary)} failed, "
                f"{sum(entry.outcome in {'skipped', 'not-attempted'} for entry in summary)} skipped/not attempted."
            )
            self.query_one("#execution-lines", Static).update("\n".join(lines))
            self.query_one("#execution-status", Static).update("Execution finished.")

        def action_back(self) -> None:
            # The executor observes this flag between targets; it never
            # interrupts a package operation that has already started.
            self.cancel_requested = True
            self.query_one("#execution-status", Static).update("Cancellation requested; finishing current target...")

    class DoctorScreen(TextScreen):
        """Show concise local diagnostics without running provider checks."""

        def __init__(self) -> None:
            lines = [f"Manager: {config.path}"]
            for scope in current_inventory.scopes:
                lines.append(f"{scope_name(scope.scope)} root: {'complete' if scope.complete else 'incomplete'}")
                lines.extend(scope.diagnostics)
            for target in current_inventory.targets:
                if target.diagnostics:
                    lines.append(f"{target.target_id}: " + "; ".join(target.diagnostics))
            super().__init__("Doctor", "\n".join(lines) or "No diagnostics.")

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

        def on_mount(self) -> None:
            """Start at the manager home screen."""
            self.push_screen(HomeScreen())

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

    while True:
        selected = ManagerApp().run()
        if selected is None:
            return 0
        run_tui(str(selected.package.root), forced_scope=selected.scope)
        # Rebuild the local records before returning to the browser so newly
        # activated versions and repaired health state are immediately visible.
        current_inventory = discover_manager(config)
