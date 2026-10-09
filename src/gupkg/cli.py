"""Parse, resolve, dispatch, and render the public ``gupkg`` command line.

The command line has one explicit parser tree. Package commands resolve one
package path, manager commands load only the selected manager configuration,
and domain workflows return result objects that are rendered here. TOML mode
therefore remains parseable even when a workflow reports an expected failure.

Usage and API
-------------
Call ``main(...)`` from the console script or with ``python -m gupkg``. The
module is the only command-line boundary; package workflows remain available
from :mod:`gupkg.gupkg` for Python callers.

Implementation Approach
-----------------------
Argparse owns the complete grammar and parses once. Resolution then selects a
package or manager context, invokes the domain operation behind an
exception-to-exit-code boundary, and emits either human output (live progress,
then a status line, records, warnings, and errors) or one versioned TOML
document with progress suppressed. Configuration repair constructs and
validates its replacement before creating a backup or changing the destination.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import gupkg as package_workflows
from ._version import __version__
from .configuration import normalize_runtime_config, validate_runtime_config
from .core import (
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    write_text_atomic,
)
from .layout import inspect_current, resolve_input_path
from .legacy_to_gupkg_toml import (
    build_config,
    pick_all_matching,
    pick_legacy_metadata_files,
    render_gupkg_toml,
)
from .manager import (
    ManagerConfig,
    discover_manager,
    discover_manager_config,
    execute_upgrade_plan,
    installation_context,
    load_manager_config,
    manager_config_candidates,
    manager_download_target,
    manager_revalidate_target,
    manager_update_target,
    manager_upgrade_target,
    plan_upgrade_all,
)
from .metadata import create_starter_config, sync_config_metadata_text
from .registry import registry_status, resolve_selector, search_registry, sync_registry
from .windows import wait_for_keypress


def main(argv: Sequence[str] | None = None) -> int:
    """Run one parsed gupkg invocation and return its process status.

    Parameters
    ----------
    argv : sequence of str, optional
        Arguments without the program name; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        ``0`` for success, ``2`` for a user or configuration error, ``3`` for
        a mutation failure, and ``4`` for an unexpected internal error.
    """
    parser, manager_parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "manager" and args.manager_command is None:
        manager_parser.print_help()
        return EXIT_USER_ERROR

    # Interactive commands own their terminal, so global rendering and pause
    # behavior never add text or wait around a TUI.
    interactive = args.command == "tui" or (args.command == "manager" and args.manager_command == "tui")
    try:
        if args.command == "manager":
            outcome = _manager_command(args)
        elif args.command == "config-fix":
            outcome = _config_fix(args)
        else:
            outcome = _package_command(args)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        outcome = _failure(args.command, f"Unexpected internal error: {exc}", EXIT_INTERNAL_ERROR)
    if interactive and outcome.data.get("interactive"):
        return outcome.result.exit_code

    if args.format == "toml":
        _render_toml(outcome)
    else:
        _render_human(outcome)
    if args.pause and not interactive:
        print("Press any key to continue...", file=sys.stderr)
        wait_for_keypress()
    return outcome.result.exit_code


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def _build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Build the complete parser tree without inspecting the filesystem."""
    parser = argparse.ArgumentParser(
        prog="gupkg",
        description="Local Package Manager for Windows (gupkg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Manager subcommands: manager tui, list, doctor, update, install, "
            "registry sync|status, search, and self status|repair|update.\n"
            "Use `gupkg manager --help` for command-specific options."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--scope", choices=[item.value for item in Scope], default="auto")
    parser.add_argument("--format", choices=["human", "toml"], default="human")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--allow-hook-dependency-install", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def package_command(name: str, help_text: str) -> argparse.ArgumentParser:
        """Add a package command with its optional package path."""
        command = commands.add_parser(name, help=help_text)
        command.add_argument("path", nargs="?", type=Path, help="package or version directory")
        return command

    def shim_linkage(command: argparse.ArgumentParser, default: str | None) -> None:
        """Add the native-wrapper linkage option to a command that installs wrappers."""
        command.add_argument("--shim-linkage", choices=["dynamic", "static"], default=default)

    def update_modes(command: argparse.ArgumentParser) -> None:
        """Add the mutually exclusive limits shared by package and manager updates."""
        modes = command.add_mutually_exclusive_group()
        modes.add_argument("--check-only", action="store_true")
        modes.add_argument("--download-only", action="store_true")
        command.add_argument("--no-checksum", action="store_true")

    install = package_command("install", "install one package")
    install.add_argument("--allow-downgrade", action="store_true")
    install.add_argument("--refresh-app", action="store_true")
    install.add_argument("--no-checksum", action="store_true")
    shim_linkage(install, "dynamic")

    update = package_command("update", "check, stage, or install a package update")
    update_modes(update)
    shim_linkage(update, "dynamic")

    package_command("config-check", "validate package configuration")

    fix = package_command("config-fix", "repair or convert package configuration")
    backup = fix.add_mutually_exclusive_group()
    backup.add_argument("--no-backup", dest="backup", action="store_false")
    backup.add_argument("--backup=false", dest="backup", action="store_false")
    fix.set_defaults(backup=True)
    fix.add_argument("--import-shortcuts", choices=["true", "false"], default="true")
    fix.add_argument("--output", type=Path)

    package_command("tui", "open package operations")

    manager = commands.add_parser("manager", help="manage configured package roots")
    manager.add_argument("--config", type=Path)
    manager.add_argument("--max-depth", type=int, default=8)
    manager_commands = manager.add_subparsers(dest="manager_command")
    manager_commands.add_parser("tui", help="open the manager interface")
    listed = manager_commands.add_parser("list", help="list managed packages")
    listed.add_argument(
        "--filter",
        choices=["all", "installed", "uninstalled", "updatable", "unhealthy"],
        default="all",
    )
    manager_commands.add_parser("doctor", help="inspect configured roots")
    manager_update = manager_commands.add_parser("update", help="update managed packages")
    update_modes(manager_update)
    manager_update.add_argument("--yes", action="store_true")
    shim_linkage(manager_update, None)
    manager_install = manager_commands.add_parser("install", help="install one registry selector")
    manager_install.add_argument("selector")
    manager_install.add_argument("--offline", action="store_true")
    registry = manager_commands.add_parser("registry", help="manage registry cache")
    registry_commands = registry.add_subparsers(dest="registry_command", required=True)
    registry_commands.add_parser("sync")
    registry_commands.add_parser("status")
    search = manager_commands.add_parser("search", help="search registry cache")
    search.add_argument("query", nargs="?")
    search.add_argument("--offline", action="store_true")
    self_parser = manager_commands.add_parser("self", help="inspect the standalone runtime")
    self_commands = self_parser.add_subparsers(dest="self_command", required=True)
    self_commands.add_parser("status")
    for name in ("repair", "update"):
        shim_linkage(self_commands.add_parser(name), None)
    return parser, manager


# ---------------------------------------------------------------------------
# Outcomes and rendering
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    """Hold one command result and its optional command-specific records."""

    command: str
    result: ActionResult
    data: dict[str, Any] = field(default_factory=dict)
    captured: str = ""


def _failure(command: str, message: str, code: int = EXIT_USER_ERROR) -> _Outcome:
    """Create a failed outcome without touching package state."""
    return _Outcome(command, ActionResult(ok=False, errors=[message], exit_code=code))


def _invoke(command: str, operation: Callable[[], ActionResult], args: argparse.Namespace) -> _Outcome:
    """Run a domain operation, capturing its progress output in TOML mode.

    Expected exception types map to the documented exit codes; anything else
    is an internal error.
    """
    output = io.StringIO()
    redirect = contextlib.redirect_stdout(output) if args.format == "toml" else contextlib.nullcontext()
    try:
        with redirect:
            result = operation()
    except (ConfigValidationError, ValueError, FileNotFoundError) as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_USER_ERROR)
    except OSError as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        result = ActionResult(ok=False, errors=[f"Unexpected internal error: {exc}"], exit_code=EXIT_INTERNAL_ERROR)
    return _Outcome(command, result, captured=output.getvalue())


def _status(result: ActionResult) -> str:
    """Return the reported status, defaulting to changed, current, or failed."""
    return result.status or ("failed" if not result.ok else ("changed" if result.changed else "current"))


# Record sections rendered as TOML tables (and ``section.key`` human lines).
_TABLE_SECTIONS = ("package", "config", "manager", "registry", "self", "summary")
# Record lists rendered as TOML arrays of tables, with their human prefixes.
_ARRAY_SECTIONS = (
    ("targets", "target", "target"),
    ("registry_packages", "registry.package", "registry package"),
    ("self_shims", "self.shim", "self shim"),
)


def _render_human(outcome: _Outcome) -> None:
    """Print the status line, structured records, warnings, and errors once each.

    Progress already streamed while the operation ran; manager commands also
    expose their targets and registry records so a user can act on the result
    without switching output formats.
    """
    print(f"{outcome.command}: {_status(outcome.result)}")
    for section in _TABLE_SECTIONS:
        values = outcome.data.get(section)
        if not isinstance(values, dict):
            continue
        if section == "summary":
            continue
        for key, value in values.items():
            if value is not None:
                print(f"{section}.{key}: {value}")
    for key, _, label in _ARRAY_SECTIONS:
        for record in outcome.data.get(key, []):
            details = ", ".join(f"{name}={value}" for name, value in record.items() if value not in (None, [], ""))
            print(f"{label}: {details}")
    if isinstance(outcome.data.get("summary"), dict):
        print("summary: " + ", ".join(f"{key}={value}" for key, value in outcome.data["summary"].items()))
    for warning in outcome.result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    for error in outcome.result.errors:
        print(f"ERROR: {error}", file=sys.stderr)


def _render_toml(outcome: _Outcome) -> None:
    """Render the common envelope and deterministic command-specific data."""
    result = outcome.result
    print("output_schema = 1")
    print(f"command = {_toml_value(outcome.command)}")
    print(f"ok = {_toml_value(result.ok)}")
    print(f"changed = {_toml_value(result.changed)}")
    print(f"status = {_toml_value(_status(result))}")
    print(f"exit_code = {result.exit_code}")
    print(f"warnings = {_toml_value(result.warnings)}")
    print(f"errors = {_toml_value(result.errors)}")

    def table(header: str, values: dict[str, Any]) -> None:
        """Print one table, omitting unset values."""
        print(f"\n{header}")
        for key, value in values.items():
            if value is not None:
                print(f"{key} = {_toml_value(value)}")

    for section in _TABLE_SECTIONS:
        if isinstance(outcome.data.get(section), dict):
            table(f"[{section}]", outcome.data[section])
    for key, header, _ in _ARRAY_SECTIONS:
        for record in outcome.data.get(key, []):
            table(f"[[{header}]]", record)


def _toml_value(value: Any) -> str:
    """Render one scalar or list as TOML; other values become strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(str(item)) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _normal_path(value: Path) -> str:
    """Render a path as the normalized absolute form promised by the CLI."""
    return str(Path(value).expanduser().resolve())


def _scope(value: str) -> Scope:
    """Convert the public scope spelling to the internal enum."""
    return Scope(value)


# ---------------------------------------------------------------------------
# Package commands
# ---------------------------------------------------------------------------


def _package_command(args: argparse.Namespace) -> _Outcome:
    """Resolve the package path once and dispatch one package command."""
    package_path, _, error = _resolve_package(args.path, command=args.command)
    if error is not None:
        return error
    scope = _scope(args.scope)
    hooks = args.allow_hook_dependency_install

    if args.command == "tui":
        from .dependencies import ensure_runtime_dependencies
        from .tui import run_tui

        ensure_runtime_dependencies("tui")
        code = run_tui(str(package_path))
        return _Outcome("tui", ActionResult(code == EXIT_SUCCESS, exit_code=code), data={"interactive": True})

    if args.command == "config-check":
        outcome = _invoke(
            "config-check", lambda: package_workflows.health_check_package(package_path, scope=scope), args
        )
        outcome.data["config"] = {"path": _normal_path(package_path), "operation": "check"}
        return outcome

    if args.command == "install":
        operation = lambda: package_workflows.install_package(  # noqa: E731
            package_path,
            scope=scope,
            allow_downgrade=args.allow_downgrade,
            refresh_app=args.refresh_app,
            no_checksum=args.no_checksum,
            local_deps_autoinstall=hooks,
            shim_linkage=args.shim_linkage,
        )
    elif args.check_only:
        operation = lambda: package_workflows.check_package_update(  # noqa: E731
            package_path, local_deps_autoinstall=hooks
        )
    elif args.download_only:
        operation = lambda: package_workflows.download_package_update(  # noqa: E731
            package_path, no_checksum=args.no_checksum, local_deps_autoinstall=hooks
        )
    else:
        operation = lambda: package_workflows.full_package_upgrade(  # noqa: E731
            package_path,
            scope=scope,
            no_checksum=args.no_checksum,
            local_deps_autoinstall=hooks,
            shim_linkage=args.shim_linkage,
        )
    outcome = _invoke(args.command, operation, args)
    outcome.data["package"] = _package_data(package_path, scope)
    return outcome


def _package_data(path: Path, scope: Scope) -> dict[str, Any]:
    """Describe a package after a command: the selected version and what ``current`` now activates."""
    try:
        identity, _ = resolve_input_path(path)
    except (OSError, ValueError):
        return {"path": _normal_path(path), "scope": scope.value}
    current = inspect_current(identity.package_root)
    return {
        "path": _normal_path(identity.version_path),
        "identity": identity.name,
        "scope": scope.value,
        "version": identity.version_string,
        "installed_version": current.version_path.name if current.version_path else None,
    }


def _resolve_package(
    path: Path | None,
    *,
    command: str,
    allow_legacy_directory: bool = False,
) -> tuple[Path | None, PackageIdentity | None, _Outcome | None]:
    """Classify one package argument and resolve its version context.

    Parameters
    ----------
    path : Path, optional
        Explicit package argument, or ``None`` for the current directory.
    command : str
        Command name used in a structured resolution failure.
    allow_legacy_directory : bool, default=False
        Whether ``config-fix`` may select a recognized legacy directory that
        has no valid version identity.

    Returns
    -------
    tuple[Path | None, PackageIdentity | None, _Outcome | None]
        The selected directory, its identity when available, and a failure
        outcome when the layout cannot be selected safely.
    """
    candidate = (path or Path.cwd()).expanduser()
    try:
        identity, _ = resolve_input_path(candidate)
        return identity.version_path, identity, None
    except OSError as exc:
        return None, None, _failure(command, str(exc))
    except ValueError as exc:
        resolution_error = str(exc)

    # Ambiguous package roots and broken ``current`` entries are never
    # reinterpreted as legacy directories.
    if (
        not allow_legacy_directory
        or "multiple version directories" in resolution_error
        or '"current" path' in resolution_error
    ):
        return None, None, _failure(command, resolution_error)
    candidate = candidate.resolve()
    if not candidate.is_dir():
        return None, None, _failure(command, f"Package directory does not exist: {candidate}")
    if not pick_legacy_metadata_files(candidate) and not _legacy_component_files(candidate):
        return None, None, _failure(command, resolution_error)
    return candidate, None, None


def _legacy_component_files(directory: Path) -> list[Path]:
    """Return recognized legacy per-component metadata files in *directory*."""
    return [
        source
        for prefix in ("environment", "env", "shortcut", "path", "bin")
        for source in pick_all_matching(directory, prefix)
    ]


def _config_fix(args: argparse.Namespace) -> _Outcome:
    """Synchronize current metadata, convert legacy metadata, or create a starter file."""
    directory, identity, error = _resolve_package(
        args.path, command="config-fix", allow_legacy_directory=True
    )
    if error is not None:
        return error
    assert directory is not None

    # Select exactly one repair mode before any work, and reject options that
    # do not apply to it.
    destination = directory / "pkg.toml"
    if destination.exists():
        operation = "synchronize"
    elif pick_legacy_metadata_files(directory) or _legacy_component_files(directory):
        operation = "legacy-conversion"
    else:
        operation = "starter"
    if args.output is not None and operation != "legacy-conversion":
        return _failure("config-fix", "--output is valid only when converting legacy metadata")
    if args.import_shortcuts != "true" and operation != "synchronize":
        return _failure("config-fix", "--import-shortcuts applies only to current canonical metadata")
    if operation != "legacy-conversion" and identity is None:
        return _failure("config-fix", "Canonical metadata requires a valid version directory")

    warnings: list[str] = []
    imported_shortcuts = False
    try:
        # Render the complete replacement document for the selected mode.
        if operation == "legacy-conversion":
            if args.output is not None:
                destination = args.output.expanduser().resolve()
            # The converter reports best-effort field diagnostics on stdout;
            # capture them so they become warnings in both output formats.
            converter_output = io.StringIO()
            with contextlib.redirect_stdout(converter_output):
                rendered = render_gupkg_toml(build_config(directory))
            warnings = [line.strip() for line in converter_output.getvalue().splitlines() if line.strip()]
        elif operation == "synchronize":
            rendered, _ = sync_config_metadata_text(destination.read_text(encoding="utf-8"), identity)
            shortcuts_dir = directory / "_shortcuts"
            if args.import_shortcuts == "true" and shortcuts_dir.is_dir():
                from .shortcuts_to_gupkg_toml import (
                    package_path_context,
                    read_shortcut_directory,
                    replace_shortcut_tables,
                )

                imported = read_shortcut_directory(shortcuts_dir, package_path_context(directory))
                if imported:
                    rendered = replace_shortcut_tables(rendered, imported)
                    imported_shortcuts = True
        else:
            rendered = create_starter_config(identity)

        # Validate the whole replacement, including preserved unrelated
        # fields, before creating a backup or touching the destination.
        _validate_replacement(rendered, identity, legacy=operation == "legacy-conversion")
        config_data = {"path": _normal_path(destination), "operation": operation}
        if destination.exists() and destination.read_text(encoding="utf-8") == rendered:
            return _Outcome(
                "config-fix",
                ActionResult(ok=True, warnings=warnings, status="unchanged"),
                data={"config": config_data},
            )

        # Back up, replace atomically, and consume imported shortcuts only
        # after the replacement succeeded.
        backup_path = None
        if args.backup and destination.exists():
            backup_path = _backup_name(destination)
            shutil.copy2(destination, backup_path)
        write_text_atomic(destination, rendered)
        if imported_shortcuts:
            from .shortcuts_to_gupkg_toml import archive_imported_shortcuts

            archive_imported_shortcuts(directory / "_shortcuts")
        config_data["backup_path"] = _normal_path(backup_path) if backup_path else None
        return _Outcome(
            "config-fix",
            ActionResult(ok=True, changed=True, warnings=warnings, status="fixed"),
            data={"config": config_data},
        )
    except (ConfigValidationError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        return _failure("config-fix", str(exc))
    except OSError as exc:
        return _failure("config-fix", str(exc), EXIT_MUTATION_ERROR)


def _validate_replacement(rendered: str, identity: PackageIdentity | None, *, legacy: bool) -> None:
    """Require a replacement ``pkg.toml`` to be one valid current package document.

    Within a version directory the full runtime schema is validated. A legacy
    conversion must additionally carry well-typed package metadata, since the
    converter infers it best-effort.
    """
    parsed = tomllib.loads(rendered)
    if identity is not None:
        validate_runtime_config(normalize_runtime_config(parsed, identity))
    if not legacy:
        return
    expected_types = {"name": str, "version": str, "localVersion": int, "only_portable": bool}
    for key, expected in expected_types.items():
        value = parsed.get(key)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigValidationError(
                "Legacy metadata did not produce one unambiguous current package document: "
                f"missing or invalid {key}"
            )


def _backup_name(destination: Path) -> Path:
    """Choose an unused ``<name>.bak.<UTC timestamp>[.N]`` sibling backup path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = destination.with_name(f"{destination.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.bak.{stamp}.{suffix}")
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# Manager commands
# ---------------------------------------------------------------------------


def _manager_command(args: argparse.Namespace) -> _Outcome:
    """Resolve manager configuration and dispatch its explicit subcommand."""
    if args.max_depth < 1:
        return _failure("manager", "--max-depth must be at least 1")
    manager, error = _manager_config(args)
    if error is not None:
        return error
    assert manager is not None

    # Registry and self workflows use only their own configured state, so a
    # missing or inaccessible root cannot hide a usable registry or runtime.
    if args.manager_command == "install":
        return _manager_install(args, manager)
    if args.manager_command == "registry":
        return _registry_sync(args, manager) if args.registry_command == "sync" else _registry_status(manager)
    if args.manager_command == "search":
        return _registry_search(args, manager)
    if args.manager_command == "self":
        return _manager_self(args, manager)

    try:
        inventory = discover_manager(manager, max_depth=args.max_depth)
    except (OSError, ValueError, ConfigValidationError) as exc:
        return _failure("manager", str(exc))
    if args.manager_command == "tui":
        from .dependencies import ensure_runtime_dependencies
        from .manager_tui import run_manager_tui

        ensure_runtime_dependencies("tui")
        code = run_manager_tui(manager, inventory)
        return _Outcome(
            "manager.tui", ActionResult(code == EXIT_SUCCESS, exit_code=code), data={"interactive": True}
        )
    if args.manager_command == "update":
        return _manager_update(args, manager, inventory)
    return _manager_list(args, inventory)


def _manager_config(args: argparse.Namespace) -> tuple[ManagerConfig | None, _Outcome | None]:
    """Load the selected manager file and report fixed-location failures."""
    selected = discover_manager_config(args.config)
    if selected is None:
        searched = ", ".join(str(path) for path in manager_config_candidates())
        return None, _failure("manager", f"No manager configuration found; searched: {searched}")
    try:
        return load_manager_config(selected), None
    except (ConfigValidationError, OSError, ValueError) as exc:
        return None, _failure("manager", str(exc))


def _selected_scopes(value: str) -> set[Scope]:
    """Resolve aggregate manager scope, where auto means both roots."""
    return {Scope.USER, Scope.MACHINE} if value == "auto" else {_scope(value)}


def _target_record(target: Any, result: ActionResult | None = None) -> dict[str, Any]:
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


def _manager_list(args: argparse.Namespace, inventory: Any) -> _Outcome:
    """Render filtered manager inventory (or doctor health) without contacting providers."""
    scopes = _selected_scopes(args.scope)
    selected = [target for target in inventory.targets if target.scope in scopes]
    if args.manager_command == "list":
        filters = {
            "all": lambda target: True,
            "installed": lambda target: target.installation_status == "installed",
            "uninstalled": lambda target: target.installation_status == "not-installed",
            "updatable": lambda target: target.update_status == "available",
            "unhealthy": lambda target: target.health_status != "healthy",
        }
        selected = [target for target in selected if filters[args.filter](target)]
    selected.sort(key=lambda item: (item.target_id.casefold(), item.target_id))
    targets = [_target_record(target) for target in selected]

    # Doctor additionally requires every selected target to be healthy.
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    if args.manager_command == "doctor":
        complete = complete and all(target["health"] == "healthy" for target in targets)
    return _Outcome(
        f"manager.{args.manager_command}",
        ActionResult(ok=complete, exit_code=EXIT_SUCCESS if complete else EXIT_USER_ERROR, status="listed"),
        data={"manager": {"complete": complete, "target_count": len(targets)}, "targets": targets},
    )


def _manager_update(args: argparse.Namespace, manager: ManagerConfig, inventory: Any) -> _Outcome:
    """Plan and execute manager updates while retaining every target result.

    The manager domain owns eligibility, revalidation, and per-target failure
    handling; this wrapper adds the one interactive confirmation required by
    a full update and translates entries into result records.
    """
    hooks = args.allow_hook_dependency_install
    plan = plan_upgrade_all(
        inventory,
        _selected_scopes(args.scope),
        lambda target: manager_update_target(target, allow_dependencies=hooks),
    )

    # Only a full update may ask for confirmation; checks and staging remain
    # suitable for automation and never prompt.
    confirmation_error: str | None = None
    if not args.check_only and not args.download_only and not args.yes:
        if any(entry.outcome == "eligible" for entry in plan.entries):
            if args.format == "toml":
                confirmation_error = "manager update requires --yes when --format toml is selected"
            elif not sys.stdin.isatty():
                confirmation_error = "manager update requires --yes in non-interactive mode"
            elif input("Run planned updates? [y/N] ").strip().casefold() not in {"y", "yes"}:
                confirmation_error = "Update cancelled"
    if confirmation_error is not None:
        # Replace the stored successful check result so a declined
        # confirmation cannot look like a successful update.
        for entry in plan.entries:
            if entry.outcome == "eligible":
                entry.result, entry.outcome, entry.reason = None, "failed", confirmation_error
    elif not args.check_only:
        if args.download_only:
            def operation(target: Any) -> ActionResult:
                return manager_download_target(target, no_checksum=args.no_checksum, allow_dependencies=hooks)
        else:
            def operation(target: Any) -> ActionResult:
                return manager_upgrade_target(
                    target,
                    manager,
                    no_checksum=args.no_checksum,
                    allow_dependencies=hooks,
                    shim_linkage=args.shim_linkage,
                )
        execute_upgrade_plan(plan, lambda target: manager_revalidate_target(target, manager), operation)

    # Every planned target appears in the report, including skipped ones.
    records: list[dict[str, Any]] = []
    for entry in plan.entries:
        result = entry.result
        if result is None and entry.outcome == "failed":
            result = ActionResult(ok=False, errors=[entry.reason or "manager target failed"], exit_code=EXIT_USER_ERROR)
        record = _target_record(entry.target, result)
        if result is None:
            record["status"] = entry.reason or entry.outcome
        if entry.target.candidate_version is not None:
            record["candidate_version"] = entry.target.candidate_version
        records.append(record)
    highest = max([EXIT_SUCCESS, *(record["exit_code"] for record in records)])
    status = "checked" if args.check_only else ("downloaded" if args.download_only else "updated")
    return _Outcome(
        "manager.update",
        ActionResult(
            ok=highest == EXIT_SUCCESS,
            changed=any(record["changed"] for record in records),
            exit_code=highest,
            status=status,
        ),
        data={
            "manager": {"target_count": len(records), "scope": args.scope},
            "targets": records,
            "summary": {
                "total": len(records),
                "changed": sum(record["changed"] for record in records),
                "failed": sum(record["exit_code"] != 0 for record in records),
            },
        },
    )


def _registry_sync(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Synchronize the registry cache from the official repository."""
    outcome = _invoke("manager.registry.sync", lambda: sync_registry(manager.registry_cache), args)
    outcome.data["registry"] = {"cache": _normal_path(manager.registry_cache)}
    return outcome


def _registry_status(manager: ManagerConfig) -> _Outcome:
    """Report the active cached registry revision without contacting Git."""
    state = registry_status(manager.registry_cache)
    return _Outcome(
        "manager.registry.status",
        ActionResult(True, status="current"),
        data={"registry": {"cache": _normal_path(manager.registry_cache), "revision": state.revision, "source": state.source}},
    )


def _registry_search(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Search the cached registry, synchronizing first only when no tree exists."""
    cache = manager.registry_cache
    if registry_status(cache).tree_path is None and not args.offline:
        sync = _invoke("manager.search", lambda: sync_registry(cache), args)
        if not sync.result.ok:
            return sync
    try:
        matches = search_registry(cache, args.query or "")
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return _failure("manager.search", str(exc))
    return _Outcome(
        "manager.search",
        ActionResult(True, status="searched"),
        data={
            "registry": {"cache": _normal_path(cache), "revision": registry_status(cache).revision},
            "registry_packages": [
                {"selector": item.selector, "path": _normal_path(item.version_path)} for item in matches
            ],
        },
    )


def _manager_install(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Resolve an exact registry selector, seed it into a root, and install it."""
    if args.scope == "auto":
        return _failure("manager.install", "manager install requires --scope user or --scope system")
    selector = args.selector
    if (
        any(part in selector for part in ("/", "\\"))
        or Path(selector).is_absolute()
        or Path(selector).drive
        or selector in {".", "..", "~"}
    ):
        return _failure("manager.install", "Registry selectors cannot be paths")
    cache = manager.registry_cache
    try:
        # Online mode synchronizes only when no cached tree exists; offline
        # mode never contacts the network.
        if registry_status(cache).tree_path is None:
            if args.offline:
                return _failure("manager.install", "--offline requires a usable cached registry tree")
            sync = _invoke("manager.install", lambda: sync_registry(cache), args)
            if not sync.result.ok:
                return sync
        package = resolve_selector(cache, selector)

        # Stage the validated seed below the selected root, then hand it to
        # the ordinary installer with manager-owned destinations.
        scope = _scope(args.scope)
        target = manager.root(scope) / package.selector / package.version_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(package.version_path, target)
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return _failure("manager.install", str(exc))
    except OSError as exc:
        return _failure("manager.install", str(exc), EXIT_MUTATION_ERROR)
    outcome = _invoke(
        "manager.install",
        lambda: package_workflows.install_package(
            target,
            scope=scope,
            install_context=installation_context(manager, scope),
            local_deps_autoinstall=args.allow_hook_dependency_install,
            shim_linkage=manager.shim_linkage,
        ),
        args,
    )
    outcome.data["manager"] = {"selector": package.selector, "scope": args.scope, "path": _normal_path(target)}
    return outcome


def _manager_self(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Report on, or repair, the standalone runtime and its scope shims."""
    from .distribution import repair_self, self_status

    command = f"manager.self.{args.self_command}"
    if args.self_command == "status":
        try:
            report = self_status()
        except (OSError, ConfigValidationError, ValueError) as exc:
            return _failure(command, str(exc), EXIT_MUTATION_ERROR)
        healthy = report["runtime_healthy"]
        return _Outcome(
            command,
            ActionResult(
                healthy,
                status="healthy" if healthy else "unhealthy",
                exit_code=EXIT_SUCCESS if healthy else EXIT_MUTATION_ERROR,
            ),
            data=_self_report_data(report),
        )

    # Repair and update both reinstall the running version; without an
    # explicit scope they target the user scope.
    scope = _scope(args.scope if args.scope != "auto" else "user")
    outcome = _invoke(
        command,
        lambda: repair_self(
            scope=scope,
            install_context=installation_context(manager, scope, shim_linkage=args.shim_linkage),
        ),
        args,
    )
    try:
        outcome.data = _self_report_data(self_status(), operation=args.self_command)
    except (OSError, ConfigValidationError, ValueError):
        outcome.data = {"self": {"operation": args.self_command}}
    return outcome


def _self_report_data(report: dict[str, Any], *, operation: str | None = None) -> dict[str, Any]:
    """Serialize standalone runtime and shim diagnostics for both renderers."""
    self_data: dict[str, Any] = {"operation": operation}
    for key in ("version_root", "runtime"):
        if report.get(key) is not None:
            self_data[key] = _normal_path(Path(report[key]))
    if "runtime_healthy" in report:
        self_data["runtime_healthy"] = bool(report["runtime_healthy"])
    shims = sorted(
        (
            {
                "scope": shim.scope.value,
                "path": _normal_path(shim.path),
                "config_path": _normal_path(shim.config_path),
                "target": shim.target,
                "healthy": bool(shim.healthy),
                "diagnostic": shim.diagnostic,
            }
            for shim in report.get("shims", [])
        ),
        key=lambda item: (item["scope"], item["path"]),
    )
    return {"self": self_data, "self_shims": shims}


if __name__ == "__main__":
    raise SystemExit(main())
