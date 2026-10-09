"""Run every gupkg operation and return its :class:`~gupkg.outcome.Outcome`.

This module is the single application layer behind all front ends. The
command line, the package TUI, and the manager TUI only collect input (arguments
or menu selections), call a function here, and display the result with the
formatters in :mod:`gupkg.outcome`; they contain no workflow logic of their own.

Usage and API
-------------
Package operations: build a :class:`PackageRequest` and call
``run_package_command(...)``; ``describe_package(...)`` and
``automatic_scope(...)`` supply the summary and scope a UI shows.

Manager operations: ``load_manager(...)``, ``init_manager(...)``,
``save_manager_config(...)``, ``list_outcome(...)``, then ``plan_updates(...)``,
``execute_updates(...)`` and ``update_outcome(...)`` for bulk updates, plus
``registry_*`` and ``install_registry_package(...)`` for the registry and
``self_outcome(...)`` for gupkg's own installation.

Implementation Approach
-----------------------
Operations take plain values and return outcomes instead of printing or
exiting. Workflows that log progress run with standard output redirected to a
caller-supplied stream, so a front end can discard it (machine output) or show
it live (a TUI). Exceptions at the boundary become failed outcomes carrying the
documented exit codes.
"""

from __future__ import annotations

import contextlib
import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from . import gupkg as package_workflows
from .configfix import fix_package_config, has_legacy_metadata
from .configuration import check_metadata_consistency
from .core import (
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    read_toml_file,
    write_text_atomic,
)
from .layout import inspect_current, resolve_input_path
from .legacy_to_gupkg_toml import pick_legacy_metadata_files
from .manager import (
    ManagerConfig,
    ManagerInventory,
    UpgradePlan,
    default_manager_config,
    discover_manager,
    discover_manager_config,
    execute_upgrade_plan,
    installation_context,
    load_manager_config,
    manager_config_candidates,
    manager_config_text,
    manager_download_target,
    manager_revalidate_target,
    manager_update_target,
    manager_upgrade_target,
    plan_upgrade_all,
)
from .outcome import Outcome, failure
from .registry import registry_status, resolve_selector, search_registry, sync_registry


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def run_guarded(
    command: str, operation: Callable[[], ActionResult], output: TextIO | None = None
) -> Outcome:
    """Run a workflow, mapping exceptions to documented exit codes.

    Parameters
    ----------
    command : str
        Command name reported in the outcome.
    operation : callable
        Workflow returning an :class:`ActionResult`.
    output : TextIO, optional
        Stream that receives the workflow's progress text; ``None`` leaves
        standard output untouched.
    """
    redirect = contextlib.redirect_stdout(output) if output is not None else contextlib.nullcontext()
    try:
        with redirect:
            result = operation()
    except (ConfigValidationError, ValueError, FileNotFoundError) as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_USER_ERROR)
    except OSError as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        result = ActionResult(
            ok=False, errors=[f"Unexpected internal error: {exc}"], exit_code=EXIT_INTERNAL_ERROR
        )
    return Outcome(command, result)


def _path_text(value: Path) -> str:
    """Render a path as the normalized absolute form promised in results."""
    return str(Path(value).expanduser().resolve())


# ---------------------------------------------------------------------------
# Package operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageRequest:
    """Describe one package operation exactly as a front end collected it.

    ``command`` is ``install``, ``update``, ``config-check``, or ``config-fix``.
    ``path`` is ``None`` for the current directory.
    """

    command: str
    path: Path | None = None
    scope: Scope = Scope.AUTO
    check_only: bool = False
    download_only: bool = False
    allow_downgrade: bool = False
    refresh_app: bool = False
    no_checksum: bool = False
    allow_hook_dependency_install: bool = False
    shim_linkage: str = "dynamic"
    backup: bool = True
    import_shortcuts: bool = True
    output: Path | None = None

    def command_line(self) -> str:
        """Return the equivalent ``gupkg`` command line, for display only."""
        parts = ["gupkg"]
        if self.scope != Scope.AUTO:
            parts += ["--scope", self.scope.value]
        if self.allow_hook_dependency_install:
            parts.append("--allow-hook-dependency-install")
        parts.append(self.command)
        if self.command == "install":
            parts += [f"--{flag}" for flag, on in (
                ("allow-downgrade", self.allow_downgrade),
                ("refresh-app", self.refresh_app),
                ("no-checksum", self.no_checksum),
            ) if on]
            parts += ["--shim-linkage", self.shim_linkage]
        elif self.command == "update":
            parts += [f"--{flag}" for flag, on in (
                ("check-only", self.check_only),
                ("download-only", self.download_only),
                ("no-checksum", self.no_checksum),
            ) if on]
            if not (self.check_only or self.download_only):
                parts += ["--shim-linkage", self.shim_linkage]
        elif self.command == "config-fix":
            if not self.backup:
                parts.append("--no-backup")
            parts += ["--import-shortcuts", "true" if self.import_shortcuts else "false"]
            if self.output is not None:
                parts += ["--output", str(self.output)]
        if self.path is not None:
            parts.append(str(self.path))
        return " ".join(parts)


class LineStream(io.TextIOBase):
    """Text stream that hands each completed line to a callback.

    Pass it as the ``output`` of an operation to show progress live (for
    example in a TUI) instead of discarding or buffering it.
    """

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self._pending = ""

    def writable(self) -> bool:
        """Report that the stream accepts text."""
        return True

    def write(self, text: str) -> int:
        """Buffer *text* and emit every complete line."""
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._on_line(line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        """Emit any trailing partial line."""
        if self._pending:
            self._on_line(self._pending.rstrip("\r"))
            self._pending = ""


def resolve_package(
    path: Path | None, *, command: str, allow_legacy_directory: bool = False
) -> tuple[Path | None, PackageIdentity | None, Outcome | None]:
    """Classify a package argument and resolve its version directory.

    Parameters
    ----------
    path : Path, optional
        Explicit package argument, or ``None`` for the current directory.
    command : str
        Command name used in a failure outcome.
    allow_legacy_directory : bool, default=False
        Whether ``config-fix`` may select a recognized legacy directory that
        has no valid version identity.

    Returns
    -------
    tuple
        ``(directory, identity, failure_outcome)``; exactly one of ``directory``
        and ``failure_outcome`` is ``None``.
    """
    candidate = (path or Path.cwd()).expanduser()
    try:
        identity, _ = resolve_input_path(candidate)
        return identity.version_path, identity, None
    except OSError as exc:
        return None, None, failure(command, str(exc))
    except ValueError as exc:
        resolution_error = str(exc)

    # Ambiguous package roots and broken ``current`` entries are never
    # reinterpreted as legacy directories.
    if (
        not allow_legacy_directory
        or "multiple version directories" in resolution_error
        or '"current" path' in resolution_error
    ):
        return None, None, failure(command, resolution_error)
    candidate = candidate.resolve()
    if not candidate.is_dir():
        return None, None, failure(command, f"Package directory does not exist: {candidate}")
    if not (pick_legacy_metadata_files(candidate) or has_legacy_metadata(candidate)):
        return None, None, failure(command, resolution_error)
    return candidate, None, None


def run_package_command(request: PackageRequest, *, output: TextIO | None = None) -> Outcome:
    """Run one package operation and describe its result.

    Parameters
    ----------
    request : PackageRequest
        The operation and its options.
    output : TextIO, optional
        Receives progress text while the operation runs (see :func:`run_guarded`).

    Returns
    -------
    Outcome
        Result with a ``package`` or ``config`` record for display.
    """
    command = request.command
    directory, identity, error = resolve_package(
        request.path, command=command, allow_legacy_directory=command == "config-fix"
    )
    if error is not None:
        return error
    assert directory is not None

    if command == "config-fix":
        return run_guarded_outcome(
            lambda: fix_package_config(
                directory,
                identity,
                backup=request.backup,
                import_shortcuts=request.import_shortcuts,
                output=request.output,
            ),
            command,
            output,
        )
    hooks = request.allow_hook_dependency_install
    if command == "config-check":
        outcome = run_guarded(
            command, lambda: package_workflows.health_check_package(directory, scope=request.scope), output
        )
        outcome.data["config"] = {"path": _path_text(directory), "operation": "check"}
        return outcome
    if command == "install":
        operation = lambda: package_workflows.install_package(  # noqa: E731
            directory,
            scope=request.scope,
            allow_downgrade=request.allow_downgrade,
            refresh_app=request.refresh_app,
            no_checksum=request.no_checksum,
            local_deps_autoinstall=hooks,
            shim_linkage=request.shim_linkage,
        )
    elif command == "update" and request.check_only:
        operation = lambda: package_workflows.check_package_update(  # noqa: E731
            directory, local_deps_autoinstall=hooks
        )
    elif command == "update" and request.download_only:
        operation = lambda: package_workflows.download_package_update(  # noqa: E731
            directory, no_checksum=request.no_checksum, local_deps_autoinstall=hooks
        )
    elif command == "update":
        operation = lambda: package_workflows.full_package_upgrade(  # noqa: E731
            directory,
            scope=request.scope,
            no_checksum=request.no_checksum,
            local_deps_autoinstall=hooks,
            shim_linkage=request.shim_linkage,
        )
    else:
        return failure(command, f"Unsupported package command: {command}")
    outcome = run_guarded(command, operation, output)
    outcome.data["package"] = _package_record(directory, request.scope)
    return outcome


def run_guarded_outcome(
    operation: Callable[[], Outcome], command: str, output: TextIO | None = None
) -> Outcome:
    """Run an operation that already returns an outcome, with progress redirected."""
    redirect = contextlib.redirect_stdout(output) if output is not None else contextlib.nullcontext()
    try:
        with redirect:
            return operation()
    except OSError as exc:
        return failure(command, str(exc), EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        return failure(command, f"Unexpected internal error: {exc}", EXIT_INTERNAL_ERROR)


def _package_record(path: Path, scope: Scope) -> dict[str, Any]:
    """Describe a package after a command: the selected version and what ``current`` activates."""
    try:
        identity, _ = resolve_input_path(path)
    except (OSError, ValueError):
        return {"path": _path_text(path), "scope": scope.value}
    current = inspect_current(identity.package_root)
    return {
        "path": _path_text(identity.version_path),
        "identity": identity.name,
        "scope": scope.value,
        "version": identity.version_string,
        "installed_version": current.version_path.name if current.version_path else None,
    }


@dataclass(frozen=True)
class PackageSummary:
    """Display text for a selected package: title, description, and any warning."""

    title: str
    description: str
    warning: str


def describe_package(path_text: str) -> PackageSummary:
    """Summarize the package at *path_text* (empty means the current directory)."""
    try:
        identity, _ = resolve_input_path(Path(path_text or ".").expanduser())
        config_path = identity.version_path / "pkg.toml"
        config = read_toml_file(config_path) if config_path.exists() else {}
        installed = "not installed"
        try:
            current_identity, _ = resolve_input_path(identity.package_root)
            if current_identity.is_current:
                installed = current_identity.version_string
        except (OSError, ValueError):
            pass
        conflicts = check_metadata_consistency(identity, config)
        description = config.get("description", "")
        return PackageSummary(
            f"{identity.name} {identity.version_string}  Installed: {installed}",
            description if isinstance(description, str) else "",
            "Warning: pkg.toml metadata conflicts with the directory name." if conflicts else "",
        )
    except (OSError, TypeError, ValueError, ConfigValidationError):
        return PackageSummary("No package selected", "Enter a package path to see its summary.", "")


def automatic_scope(path_text: str) -> tuple[Scope, bool] | None:
    """Return the scope ``auto`` would pick for a package, and whether System is available."""
    from .windows import is_current_user_admin

    try:
        identity, _ = resolve_input_path(Path(path_text or ".").expanduser())
    except (OSError, ValueError):
        return None
    system_available = is_current_user_admin() and not identity.only_portable_by_name
    return (Scope.MACHINE if system_available else Scope.USER), system_available


# ---------------------------------------------------------------------------
# Manager configuration
# ---------------------------------------------------------------------------


def load_manager(explicit: Path | None = None) -> ManagerConfig:
    """Load the manager configuration from ``--config`` or the fixed search locations.

    Raises
    ------
    ConfigValidationError
        If no file is found (the message lists every searched location) or the
        selected file is invalid.
    """
    selected = discover_manager_config(explicit)
    if selected is None:
        searched = ", ".join(str(path) for path in manager_config_candidates())
        raise ConfigValidationError(
            f"No manager configuration found; searched: {searched}. "
            "Create one with 'gupkg manager init'."
        )
    return load_manager_config(selected)


def save_manager_config(config: ManagerConfig) -> ManagerConfig:
    """Write *config* atomically and return it re-read through full validation."""
    write_text_atomic(config.path, manager_config_text(config))
    return load_manager_config(config.path)


def init_manager(
    path: Path | None = None, *, force: bool = False, config: ManagerConfig | None = None
) -> tuple[ManagerConfig, list[str]]:
    """Create a manager configuration and the folders it names.

    Parameters
    ----------
    path : Path, optional
        Configuration file to create; default ``%APPDATA%\\gupkg\\gupkg-config.toml``.
    config : ManagerConfig, optional
        Reviewed or edited settings to write; the defaults when omitted.
    force : bool, default=False
        Overwrite an existing file.

    Returns
    -------
    tuple[ManagerConfig, list[str]]
        The validated configuration and warnings for folders that could not be
        created (for example the system root without Administrator rights).

    Raises
    ------
    ConfigValidationError
        If the file already exists and *force* is false, or the environment
        does not identify the default locations.
    """
    config = config or default_manager_config(path)
    if config.path.exists() and not force:
        raise ConfigValidationError(
            f"Manager configuration already exists: {config.path} (use --force to replace it)"
        )
    saved = save_manager_config(config)
    warnings: list[str] = []
    for folder in (saved.user_root, saved.user_bin, saved.system_root, saved.system_bin):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warnings.append(f"Could not create {folder}: {exc}")
    return saved, warnings


def manager_init_outcome(path: Path | None, *, force: bool) -> Outcome:
    """Run :func:`init_manager` as a command outcome."""
    try:
        config, warnings = init_manager(path, force=force)
    except (ConfigValidationError, ValueError) as exc:
        return failure("manager.init", str(exc))
    except OSError as exc:
        return failure("manager.init", str(exc), EXIT_MUTATION_ERROR)
    return Outcome(
        "manager.init",
        ActionResult(ok=True, changed=True, warnings=warnings, status="created"),
        {"manager": {
            "config": _path_text(config.path),
            "user_packages": config.user_root,
            "system_packages": config.system_root,
            "registry_cache": config.registry_cache,
        }},
    )


# ---------------------------------------------------------------------------
# Manager inventory and bulk updates
# ---------------------------------------------------------------------------


def selected_scopes(value: str) -> set[Scope]:
    """Resolve an aggregate manager scope, where ``auto`` means both roots."""
    return {Scope.USER, Scope.MACHINE} if value == "auto" else {Scope(value)}


def target_record(target: Any, result: ActionResult | None = None) -> dict[str, Any]:
    """Serialize one manager target, with its action result when one exists."""
    return {
        "id": target.target_id,
        "selector": target.package.selector,
        "scope": target.scope.value,
        "installation": target.installation_status,
        "installed_version": target.installed_version,
        "health": target.health_status,
        "status": target.update_status if result is None else (result.status or ("ok" if result.ok else "failed")),
        "changed": False if result is None else result.changed,
        "exit_code": 0 if result is None else result.exit_code,
        "warnings": [] if result is None else result.warnings,
        "errors": target.diagnostics if result is None else result.errors,
    }


def filter_targets(inventory: ManagerInventory, scopes: set[Scope], status: str = "all") -> list[Any]:
    """Return targets in *scopes* matching a status filter.

    *status* is ``all``, ``installed``, ``uninstalled``, ``updatable``, or
    ``unhealthy`` (case-insensitive).
    """
    matches = {
        "all": lambda target: True,
        "installed": lambda target: target.installation_status == "installed",
        "uninstalled": lambda target: target.installation_status == "not-installed",
        "updatable": lambda target: target.update_status == "available",
        "unhealthy": lambda target: target.health_status != "healthy",
    }[status.casefold()]
    return [t for t in inventory.targets if t.scope in scopes and matches(t)]


def diagnostic_lines(config: ManagerConfig, inventory: ManagerInventory) -> list[str]:
    """Return the local diagnostics ``manager doctor`` shows, one per line."""
    lines = [f"Manager: {config.path}"]
    for scope in inventory.scopes:
        lines.append(f"{'User' if scope.scope == Scope.USER else 'System'} root: {'complete' if scope.complete else 'incomplete'}")
        lines.extend(scope.diagnostics)
    lines.extend(
        f"{target.target_id}: " + "; ".join(target.diagnostics)
        for target in inventory.targets
        if target.diagnostics
    )
    return lines


def refresh_update_status(inventory: ManagerInventory) -> list[ActionResult]:
    """Check every target's update source one at a time and record its status.

    Checks run sequentially because each one captures process-wide output.
    """
    return [manager_update_target(target) for target in inventory.targets]


def needs_elevation(plan: UpgradePlan) -> bool:
    """Return whether the plan changes the system scope from a non-elevated process."""
    from .windows import is_current_user_admin

    return any(
        entry.outcome == "eligible" and entry.target.scope == Scope.MACHINE for entry in plan.entries
    ) and not is_current_user_admin()


def relaunch_elevated_update(
    config: ManagerConfig, *, allow_hook_dependency_install: bool, no_checksum: bool
) -> bool:
    """Ask Windows to repeat a confirmed bulk update elevated; return whether it was accepted."""
    from .windows import relaunch_elevated

    return relaunch_elevated(
        elevated_update_arguments(
            config, allow_hook_dependency_install=allow_hook_dependency_install, no_checksum=no_checksum
        )
    )


def list_outcome(
    inventory: ManagerInventory, scope: str, *, doctor: bool = False, filter_name: str = "all"
) -> Outcome:
    """Report filtered inventory (or doctor health) without contacting providers."""
    scopes = selected_scopes(scope)
    selected = sorted(
        filter_targets(inventory, scopes, filter_name),
        key=lambda item: (item.target_id.casefold(), item.target_id),
    )
    targets = [target_record(target) for target in selected]

    # Doctor additionally requires every selected target to be healthy.
    complete = all(s.complete for s in inventory.scopes if s.scope in scopes)
    if doctor:
        complete = complete and all(record["health"] == "healthy" for record in targets)
    return Outcome(
        "manager.doctor" if doctor else "manager.list",
        ActionResult(ok=complete, exit_code=EXIT_SUCCESS if complete else EXIT_USER_ERROR, status="listed"),
        {"manager": {"complete": complete, "target_count": len(targets)}, "targets": targets},
    )


def plan_updates(inventory: ManagerInventory, scope: str, *, allow_hook_dependency_install: bool = False) -> UpgradePlan:
    """Check every selected installed target and plan updates; nothing is downloaded."""
    return plan_upgrade_all(
        inventory,
        selected_scopes(scope),
        lambda target: manager_update_target(target, allow_dependencies=allow_hook_dependency_install),
    )


def has_eligible_updates(plan: UpgradePlan) -> bool:
    """Return whether the plan has anything to download or install."""
    return any(entry.outcome == "eligible" for entry in plan.entries)


def decline_updates(plan: UpgradePlan, reason: str) -> None:
    """Mark every eligible entry failed with *reason* (a declined confirmation).

    The stored successful check result is dropped so a declined or impossible
    confirmation can never look like a successful update.
    """
    for entry in plan.entries:
        if entry.outcome == "eligible":
            entry.result, entry.outcome, entry.reason = None, "failed", reason


def execute_updates(
    plan: UpgradePlan,
    config: ManagerConfig,
    *,
    download_only: bool = False,
    no_checksum: bool = False,
    allow_hook_dependency_install: bool = False,
    shim_linkage: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> UpgradePlan:
    """Update (or only stage) every eligible entry, continuing after failures.

    Parameters
    ----------
    plan : UpgradePlan
        Plan from :func:`plan_updates`; entries receive their results.
    config : ManagerConfig
        Supplies roots, bin directories, and the default shim linkage.
    download_only : bool, default=False
        Stage new versions without activating them.
    cancel_requested : callable, optional
        Checked between targets; a running target is never interrupted.
    on_progress : callable, optional
        Called as ``on_progress(target_id, "running" | "completed" | "failed")``.
    """
    def operation(target: Any) -> ActionResult:
        """Update one target, reporting its progress."""
        if on_progress:
            on_progress(target.target_id, "running")
        if download_only:
            result = manager_download_target(
                target, no_checksum=no_checksum, allow_dependencies=allow_hook_dependency_install
            )
        else:
            result = manager_upgrade_target(
                target,
                config,
                no_checksum=no_checksum,
                allow_dependencies=allow_hook_dependency_install,
                shim_linkage=shim_linkage,
            )
        if on_progress:
            on_progress(target.target_id, "completed" if result.ok else "failed")
        return result

    return execute_upgrade_plan(
        plan,
        lambda target: manager_revalidate_target(target, config),
        operation,
        cancel_requested=cancel_requested,
    )


def update_outcome(plan: UpgradePlan, *, mode: str, scope: str) -> Outcome:
    """Report every planned target's outcome.

    Parameters
    ----------
    mode : {"checked", "downloaded", "updated"}
        What the run was allowed to do; it becomes the outcome status.
    """
    records: list[dict[str, Any]] = []
    for entry in plan.entries:
        result = entry.result
        if result is None and entry.outcome == "failed":
            result = ActionResult(ok=False, errors=[entry.reason or "manager target failed"], exit_code=EXIT_USER_ERROR)
        record = target_record(entry.target, result)
        if result is None:
            record["status"] = entry.reason or entry.outcome
        if entry.target.candidate_version is not None:
            record["candidate_version"] = entry.target.candidate_version
        records.append(record)
    highest = max([EXIT_SUCCESS, *(record["exit_code"] for record in records)])
    return Outcome(
        "manager.update",
        ActionResult(
            ok=highest == EXIT_SUCCESS,
            changed=any(record["changed"] for record in records),
            exit_code=highest,
            status=mode,
        ),
        {
            "manager": {"target_count": len(records), "scope": scope},
            "targets": records,
            "summary": {
                "total": len(records),
                "changed": sum(record["changed"] for record in records),
                "failed": sum(record["exit_code"] != 0 for record in records),
            },
        },
    )


def plan_summary_lines(plan: UpgradePlan) -> list[str]:
    """Return the counts and per-target lines a front end shows before confirming."""
    entries = plan.entries
    lines = [
        f"Available: {sum(e.outcome == 'eligible' for e in entries)}",
        f"Current: {sum(e.reason == 'current' for e in entries)}",
        f"Skipped: {sum(e.outcome == 'skipped' and e.reason != 'current' for e in entries)}",
        f"Failed checks: {sum(e.outcome == 'failed' for e in entries)}",
        "",
    ]
    lines.extend(
        f"{e.target.target_id}: {e.outcome}" + (f" ({e.reason})" if e.reason else "") for e in entries
    )
    return lines


def execution_summary_line(plan: UpgradePlan) -> str:
    """Return the one-line totals shown after a batch finishes."""
    entries = plan.entries
    return (
        f"Summary: {sum(e.outcome == 'upgraded' for e in entries)} upgraded, "
        f"{sum(e.outcome == 'failed' for e in entries)} failed, "
        f"{sum(e.outcome in {'skipped', 'not-attempted'} for e in entries)} skipped/not attempted."
    )


def elevated_update_arguments(
    config: ManagerConfig, *, allow_hook_dependency_install: bool, no_checksum: bool
) -> list[str]:
    """Return the argument vector that repeats a confirmed bulk update elevated."""
    arguments = ["--allow-hook-dependency-install"] if allow_hook_dependency_install else []
    arguments += ["manager", "--config", str(config.path), "update", "--yes"]
    if no_checksum:
        arguments.append("--no-checksum")
    return arguments


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def registry_sync_outcome(config: ManagerConfig, output: TextIO | None = None) -> Outcome:
    """Download the latest registry archive into the configured cache."""
    outcome = run_guarded(
        "manager.registry.sync",
        lambda: sync_registry(config.registry_cache, source=config.registry_source),
        output,
    )
    outcome.data["registry"] = {"cache": _path_text(config.registry_cache), "source": config.registry_source}
    return outcome


def registry_status_outcome(config: ManagerConfig) -> Outcome:
    """Report the active cached registry revision without any download."""
    state = registry_status(config.registry_cache)
    return Outcome(
        "manager.registry.status",
        ActionResult(True, status="current"),
        {"registry": {
            "cache": _path_text(config.registry_cache),
            "source": config.registry_source,
            "revision": state.revision,
            "synchronized_at": state.synchronized_at,
        }},
    )


def _ensure_registry(
    command: str, config: ManagerConfig, offline: bool, output: TextIO | None
) -> Outcome | None:
    """Make sure a cached registry exists, downloading only when allowed.

    Returns a failure outcome when no usable cache is available, else ``None``.
    """
    if registry_status(config.registry_cache).tree_path is not None:
        return None
    if offline:
        return failure(command, "--offline requires a usable cached registry tree")
    sync = run_guarded(
        command,
        lambda: sync_registry(config.registry_cache, source=config.registry_source),
        output,
    )
    return None if sync.result.ok else sync


def registry_search_outcome(
    config: ManagerConfig, query: str, *, offline: bool, output: TextIO | None = None
) -> Outcome:
    """Search the registry by name, downloading it first only when none is cached."""
    problem = _ensure_registry("manager.search", config, offline, output)
    if problem is not None:
        return problem
    try:
        matches = search_registry(config.registry_cache, query)
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return failure("manager.search", str(exc))
    return Outcome(
        "manager.search",
        ActionResult(True, status="searched"),
        {
            "registry": {
                "cache": _path_text(config.registry_cache),
                "revision": registry_status(config.registry_cache).revision,
            },
            "registry_packages": [
                {"selector": item.selector, "path": _path_text(item.version_path)} for item in matches
            ],
        },
    )


def install_registry_package(
    config: ManagerConfig,
    selector: str,
    scope: Scope,
    *,
    offline: bool = False,
    allow_hook_dependency_install: bool = False,
    output: TextIO | None = None,
) -> Outcome:
    """Install one registry package by exact name into the configured root.

    Parameters
    ----------
    selector : str
        Exact (case-insensitive) registry name, never a path.
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``; a new package has no owner yet.
    offline : bool, default=False
        Require an already cached registry; never download.
    """
    command = "manager.install"
    if scope == Scope.AUTO:
        return failure(command, "manager install requires --scope user or --scope system")
    if (
        any(part in selector for part in ("/", "\\"))
        or Path(selector).is_absolute()
        or Path(selector).drive
        or selector in {".", "..", "~"}
    ):
        return failure(command, "Registry selectors cannot be paths")
    try:
        problem = _ensure_registry(command, config, offline, output)
        if problem is not None:
            return problem
        package = resolve_selector(config.registry_cache, selector)

        # Stage the validated seed below the selected root, then hand it to
        # the ordinary installer with manager-owned destinations.
        target = config.root(scope) / package.selector / package.version_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(package.version_path, target)
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return failure(command, str(exc))
    except OSError as exc:
        return failure(command, str(exc), EXIT_MUTATION_ERROR)
    outcome = run_guarded(
        command,
        lambda: package_workflows.install_package(
            target,
            scope=scope,
            install_context=installation_context(config, scope),
            local_deps_autoinstall=allow_hook_dependency_install,
            shim_linkage=config.shim_linkage,
        ),
        output,
    )
    outcome.data["manager"] = {"selector": package.selector, "scope": scope.value, "path": _path_text(target)}
    return outcome


# ---------------------------------------------------------------------------
# gupkg's own installation
# ---------------------------------------------------------------------------


def self_outcome(
    config: ManagerConfig,
    action: str,
    *,
    scope: Scope = Scope.USER,
    shim_linkage: str | None = None,
    output: TextIO | None = None,
) -> Outcome:
    """Report on (``status``) or repair (``repair``/``update``) the standalone runtime."""
    from .distribution import repair_self, self_status

    command = f"manager.self.{action}"
    if action == "status":
        try:
            report = self_status()
        except (OSError, ConfigValidationError, ValueError) as exc:
            return failure(command, str(exc), EXIT_MUTATION_ERROR)
        healthy = report["runtime_healthy"]
        return Outcome(
            command,
            ActionResult(
                healthy,
                status="healthy" if healthy else "unhealthy",
                exit_code=EXIT_SUCCESS if healthy else EXIT_MUTATION_ERROR,
            ),
            _self_record(report),
        )

    # Repair and update both reinstall the running version.
    scope = Scope.USER if scope == Scope.AUTO else scope
    outcome = run_guarded(
        command,
        lambda: repair_self(
            scope=scope, install_context=installation_context(config, scope, shim_linkage=shim_linkage)
        ),
        output,
    )
    try:
        outcome.data = _self_record(self_status(), operation=action)
    except (OSError, ConfigValidationError, ValueError):
        outcome.data = {"self": {"operation": action}}
    return outcome


def _self_record(report: dict[str, Any], *, operation: str | None = None) -> dict[str, Any]:
    """Serialize standalone runtime and shim diagnostics."""
    self_data: dict[str, Any] = {"operation": operation}
    for key in ("version_root", "runtime"):
        if report.get(key) is not None:
            self_data[key] = _path_text(Path(report[key]))
    if "runtime_healthy" in report:
        self_data["runtime_healthy"] = bool(report["runtime_healthy"])
    shims = sorted(
        (
            {
                "scope": shim.scope.value,
                "path": _path_text(shim.path),
                "config_path": _path_text(shim.config_path),
                "target": shim.target,
                "healthy": bool(shim.healthy),
                "diagnostic": shim.diagnostic,
            }
            for shim in report.get("shims", [])
        ),
        key=lambda item: (item["scope"], item["path"]),
    )
    return {"self": self_data, "self_shims": shims}


__all__ = [
    "PackageRequest",
    "LineStream",
    "PackageSummary",
    "automatic_scope",
    "decline_updates",
    "describe_package",
    "diagnostic_lines",
    "filter_targets",
    "needs_elevation",
    "refresh_update_status",
    "relaunch_elevated_update",
    "discover_manager",
    "elevated_update_arguments",
    "execute_updates",
    "execution_summary_line",
    "has_eligible_updates",
    "init_manager",
    "install_registry_package",
    "list_outcome",
    "load_manager",
    "manager_init_outcome",
    "plan_summary_lines",
    "plan_updates",
    "registry_search_outcome",
    "registry_status_outcome",
    "registry_sync_outcome",
    "resolve_package",
    "run_guarded",
    "run_package_command",
    "save_manager_config",
    "selected_scopes",
    "self_outcome",
    "target_record",
    "update_outcome",
]
