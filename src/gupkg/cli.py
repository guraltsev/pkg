"""Parse, resolve, dispatch, and render the public ``gupkg`` command line.

The command line has one explicit parser tree.  Package commands resolve one
package path, manager commands load only the selected manager configuration,
and domain workflows return result objects that are rendered here.  TOML mode
therefore remains parseable even when a workflow reports an expected failure.

Usage and API
-------------
Call ``main(...)`` from the console script or with ``python -m gupkg``.  The
module is intentionally the only command-line compatibility boundary; package
workflows remain available from :mod:`gupkg.gupkg` for Python callers.

Implementation Approach
-----------------------
Argparse owns the complete grammar and parses once.  Resolution then selects a
package or manager context, invokes the existing domain operation behind a
captured-output boundary, and emits either human diagnostics or one versioned
TOML document.  Configuration repair constructs and validates its replacement
before creating a backup or changing the destination.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ._version import __version__
from .configuration import normalize_runtime_config, validate_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    write_text_atomic,
)
from . import gupkg as package_workflows
from .layout import resolve_input_path
from .legacy_to_gupkg_toml import (
    build_config,
    pick_all_matching,
    pick_legacy_metadata_files,
    render_gupkg_toml,
)
from .metadata import sync_config_metadata_text
from .manager import (
    ManagerConfig,
    discover_manager,
    discover_manager_config,
    installation_context,
    load_manager_config,
    manager_update_target,
    manager_upgrade_target,
    manager_download_target,
    manager_revalidate_target,
    plan_upgrade_all,
    execute_upgrade_plan,
    scope_name,
)
from .registry import (
    registry_status,
    resolve_selector,
    search_registry,
    sync_registry,
)
from .windows import wait_for_keypress


@dataclass
class _Outcome:
    """Hold one command result and its optional command-specific records."""

    command: str
    result: ActionResult
    data: dict[str, Any] = field(default_factory=dict)
    captured: str = ""


def _toml_string(value: str) -> str:
    """Return a TOML-compatible basic string."""
    return json.dumps(str(value), ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    """Return a deterministic TOML string array."""
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _normal_path(value: Path) -> str:
    """Render a path as the normalized absolute form promised by the CLI."""
    return str(value.expanduser().resolve())


def _result(
    command: str,
    result: ActionResult,
    *,
    data: dict[str, Any] | None = None,
    captured: str = "",
) -> _Outcome:
    """Create a renderer-ready outcome."""
    return _Outcome(command, result, data or {}, captured)


def _failure(command: str, message: str, code: int = EXIT_USER_ERROR) -> _Outcome:
    """Create a user-facing failed outcome without writing package state."""
    return _result(
        command,
        ActionResult(ok=False, errors=[message], exit_code=code),
    )


def _dispatch_failure(command: str, exc: Exception) -> _Outcome:
    """Translate a dispatcher-boundary exception into one CLI result."""
    if isinstance(exc, (ConfigValidationError, ValueError, FileNotFoundError)):
        code = EXIT_USER_ERROR
    elif isinstance(exc, OSError):
        code = EXIT_MUTATION_ERROR
    else:
        code = EXIT_INTERNAL_ERROR
    return _failure(command, str(exc), code)


def _invoke(command: str, operation: Callable[[], ActionResult]) -> _Outcome:
    """Run a domain operation while separating its legacy progress output."""
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            result = operation()
    except (ConfigValidationError, ValueError, FileNotFoundError) as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_USER_ERROR)
    except OSError as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        result = ActionResult(
            ok=False,
            errors=[f"Unexpected internal error: {exc}"],
            exit_code=EXIT_INTERNAL_ERROR,
        )
    return _result(command, result, captured=output.getvalue())


def _package_data(path: Path, scope: Scope) -> dict[str, Any]:
    """Describe a package path when its directory identity is available."""
    try:
        identity, _ = resolve_input_path(path)
    except (OSError, ValueError):
        return {"path": _normal_path(path), "scope": scope.value}
    return {
        "path": _normal_path(identity.version_path),
        "identity": identity.name,
        "scope": scope.value,
        "installed_version": identity.version_string,
    }


def _render_human(outcome: _Outcome) -> None:
    """Render one outcome with its command-specific records.

    Human output is intentionally more descriptive than the machine envelope:
    manager commands need to expose the selected targets and registry records
    so a user can act on the result without switching output formats.
    """
    def render_records() -> None:
        """Print structured records that remain useful after a partial failure."""
        for section in ("package", "config", "manager", "registry", "self"):
            values = outcome.data.get(section)
            if isinstance(values, dict):
                for key, value in values.items():
                    if value is not None:
                        print(f"{section}.{key}: {value}")
        for target in outcome.data.get("targets", []):
            details = ", ".join(
                f"{key}={value}" for key, value in target.items() if value not in (None, [], "")
            )
            print(f"target: {details}")
        for package in outcome.data.get("registry_packages", []):
            details = ", ".join(f"{key}={value}" for key, value in package.items())
            print(f"registry package: {details}")
        for shim in outcome.data.get("self_shims", []):
            details = ", ".join(
                f"{key}={value}" for key, value in shim.items() if value not in (None, "")
            )
            print(f"self shim: {details}")
        summary = outcome.data.get("summary")
        if isinstance(summary, dict):
            print("summary: " + ", ".join(f"{key}={value}" for key, value in summary.items()))

    if outcome.captured:
        print(outcome.captured, end="", file=None if outcome.result.ok else sys.stderr)
    status = outcome.result.status or (
        "changed" if outcome.result.changed else ("current" if outcome.result.ok else "failed")
    )
    print(f"{outcome.command}: {status}")
    render_records()
    if outcome.result.ok:
        for warning in outcome.result.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    else:
        for warning in outcome.result.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        for error in outcome.result.errors:
            print(f"ERROR: {error}", file=sys.stderr)


def _render_toml(outcome: _Outcome) -> None:
    """Render the common envelope and deterministic command-specific data."""
    result = outcome.result
    status = result.status or (
        "failed" if not result.ok else ("changed" if result.changed else "current")
    )
    print("output_schema = 1")
    print(f"command = {_toml_string(outcome.command)}")
    print(f"ok = {'true' if result.ok else 'false'}")
    print(f"changed = {'true' if result.changed else 'false'}")
    print(f"status = {_toml_string(status)}")
    print(f"exit_code = {result.exit_code}")
    print(f"warnings = {_toml_list(result.warnings)}")
    print(f"errors = {_toml_list(result.errors)}")
    for section in ("package", "config", "manager", "registry", "self", "summary"):
        values = outcome.data.get(section)
        if isinstance(values, dict):
            print(f"\n[{section}]")
            for key, value in values.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, int):
                    rendered = str(value)
                elif isinstance(value, list):
                    rendered = _toml_list([str(item) for item in value])
                else:
                    rendered = _toml_string(str(value))
                print(f"{key} = {rendered}")
    for target in outcome.data.get("targets", []):
        print("\n[[target]]")
        for key, value in target.items():
            if value is None:
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, list):
                rendered = _toml_list([str(item) for item in value])
            else:
                rendered = _toml_string(str(value))
            print(f"{key} = {rendered}")
    for package in outcome.data.get("registry_packages", []):
        print("\n[[registry.package]]")
        for key, value in package.items():
            print(f"{key} = {_toml_string(str(value))}")
    for shim in outcome.data.get("self_shims", []):
        print("\n[[self.shim]]")
        for key, value in shim.items():
            if value is None:
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = _toml_string(str(value))
            print(f"{key} = {rendered}")


def _add_package_path(parser: argparse.ArgumentParser) -> None:
    """Add the optional package path shared by package commands."""
    parser.add_argument("path", nargs="?", type=Path, help="package or version directory")


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

    install = commands.add_parser("install", help="install one package")
    _add_package_path(install)
    install.add_argument("--allow-downgrade", action="store_true")
    install.add_argument("--refresh-app", action="store_true")
    install.add_argument("--no-checksum", action="store_true")
    install.add_argument("--shim-linkage", choices=["dynamic", "static"], default="dynamic")

    update = commands.add_parser("update", help="check, stage, or install a package update")
    _add_package_path(update)
    update_mode = update.add_mutually_exclusive_group()
    update_mode.add_argument("--check-only", action="store_true")
    update_mode.add_argument("--download-only", action="store_true")
    update.add_argument("--no-checksum", action="store_true")
    update.add_argument("--shim-linkage", choices=["dynamic", "static"], default="dynamic")

    check = commands.add_parser("config-check", help="validate package configuration")
    _add_package_path(check)

    fix = commands.add_parser("config-fix", help="repair or convert package configuration")
    _add_package_path(fix)
    backup = fix.add_mutually_exclusive_group()
    backup.add_argument("--no-backup", dest="backup", action="store_false")
    backup.add_argument("--backup=false", dest="backup", action="store_false")
    fix.set_defaults(backup=True)
    fix.add_argument("--import-shortcuts", choices=["true", "false"], default="true")
    fix.add_argument("--output", type=Path)

    tui = commands.add_parser("tui", help="open package operations")
    _add_package_path(tui)

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
    manager_mode = manager_update.add_mutually_exclusive_group()
    manager_mode.add_argument("--check-only", action="store_true")
    manager_mode.add_argument("--download-only", action="store_true")
    manager_update.add_argument("--yes", action="store_true")
    manager_update.add_argument("--no-checksum", action="store_true")
    manager_update.add_argument("--shim-linkage", choices=["dynamic", "static"])
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
        item = self_commands.add_parser(name)
        item.add_argument("--shim-linkage", choices=["dynamic", "static"])
    return parser, manager


def _scope(value: str) -> Scope:
    """Convert the public scope spelling to the internal enum."""
    return {"auto": Scope.AUTO, "user": Scope.USER, "system": Scope.MACHINE}[value]


def _manager_config(args: argparse.Namespace) -> tuple[ManagerConfig | None, _Outcome | None]:
    """Load the selected manager file and report fixed-location failures."""
    module_directory = Path(__file__).resolve().parent
    explicit = args.config
    selected = discover_manager_config(
        explicit,
        module_directory=module_directory,
        gupkg_home=os.environ.get("GUPKG_HOME"),
        appdata=os.environ.get("APPDATA"),
    )
    if explicit is not None:
        candidates = [str(explicit.expanduser().resolve())]
    else:
        candidates = [str(module_directory / "gupkg-config.toml")]
        if os.environ.get("GUPKG_HOME"):
            candidates.append(str(Path(os.environ["GUPKG_HOME"]) / "gupkg-config.toml"))
        if os.environ.get("APPDATA"):
            candidates.append(str(Path(os.environ["APPDATA"]) / "gupkg" / "gupkg-config.toml"))
    if selected is None:
        return None, _failure(
            "manager", "No manager configuration found; searched: " + ", ".join(candidates)
        )
    try:
        return load_manager_config(selected), None
    except (ConfigValidationError, OSError, ValueError) as exc:
        return None, _failure("manager", str(exc))


def _resolve_context(
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
        Whether a config repair may select a legacy directory without a valid
        canonical version identity.

    Returns
    -------
    tuple[Path | None, PackageIdentity | None, _Outcome | None]
        The selected version path, its identity when available, and a failure
        outcome when the layout cannot be selected safely.
    """
    candidate = (path or Path.cwd()).expanduser()
    resolution_error: ValueError | None = None
    try:
        identity, _ = resolve_input_path(candidate)
        return identity.version_path, identity, None
    except (OSError, ValueError) as exc:
        if not allow_legacy_directory or not isinstance(exc, ValueError):
            return None, None, _failure(command, str(exc))
        resolution_error = exc

    if resolution_error is not None and (
        "multiple version directories" in str(resolution_error)
        or '"current" path' in str(resolution_error)
    ):
        return None, None, _failure(command, str(resolution_error))
    candidate = candidate.resolve()
    if not candidate.is_dir():
        return None, None, _failure(command, f"Package directory does not exist: {candidate}")
    legacy_sources = pick_legacy_metadata_files(candidate)
    legacy_sources.extend(
        source
        for prefix in ("environment", "env", "shortcut", "path", "bin")
        for source in pick_all_matching(candidate, prefix)
        if source not in legacy_sources
    )
    if not legacy_sources:
        return None, None, _failure(
            command,
            str(resolution_error)
            if resolution_error is not None
            else f"Path is not a valid version or recognized legacy package directory: {candidate}",
        )
    return candidate, None, None


def _resolve_package_path(path: Path | None) -> tuple[Path | None, _Outcome | None]:
    """Resolve an ordinary package command through the shared classifier."""
    version_path, _, error = _resolve_context(path, command="package")
    return version_path, error


def _repair_directory(path: Path | None) -> tuple[Path | None, PackageIdentity | None, _Outcome | None]:
    """Resolve a repair directory, including recognized legacy layouts."""
    return _resolve_context(
        path,
        command="config-fix",
        allow_legacy_directory=True,
    )


def _backup_name(destination: Path) -> Path:
    """Choose the required UTC timestamped sibling backup path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = destination.with_name(f"{destination.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.bak.{stamp}.{suffix}")
        suffix += 1
    return candidate


def _atomic_config_replacement(destination: Path, content: str, *, backup: bool) -> Path | None:
    """Back up and atomically replace a configuration after validation."""
    backup_path = None
    if backup and destination.exists():
        backup_path = _backup_name(destination)
        shutil.copy2(destination, backup_path)
    write_text_atomic(destination, content, backup=False)
    return backup_path


def _config_fix(args: argparse.Namespace) -> _Outcome:
    """Repair current metadata, convert legacy metadata, or create a starter file."""
    directory, identity, error = _repair_directory(args.path)
    if error is not None:
        return error
    assert directory is not None
    destination = directory / "pkg.toml"
    canonical_exists = destination.exists()
    legacy_exists = not canonical_exists and bool(pick_legacy_metadata_files(directory))
    if not canonical_exists and not legacy_exists:
        legacy_exists = any(
            pick_all_matching(directory, prefix)
            for prefix in ("environment", "env", "shortcut", "path", "bin")
        )
    if args.output is not None and not legacy_exists:
        return _failure("config-fix", "--output is valid only when converting legacy metadata")
    if args.import_shortcuts != "true" and not canonical_exists:
        return _failure("config-fix", "--import-shortcuts applies only to current canonical metadata")

    legacy_warnings: list[str] = []
    try:
        if legacy_exists:
            destination = args.output.expanduser().resolve() if args.output else destination
            # The established converter reports best-effort field diagnostics
            # through stdout.  Capture them so machine mode remains one TOML
            # document and human mode can render them as warnings.
            converter_output = io.StringIO()
            with contextlib.redirect_stdout(converter_output):
                rendered = render_gupkg_toml(build_config(directory))
            legacy_warnings = [
                line.strip()
                for line in converter_output.getvalue().splitlines()
                if line.strip()
            ]
            parsed_legacy = tomllib.loads(rendered)
            required = {"name", "version", "localVersion"}
            if not required.issubset(parsed_legacy) or not isinstance(parsed_legacy["name"], str) or not isinstance(parsed_legacy["version"], str):
                raise ConfigValidationError(
                    "Legacy metadata did not produce one unambiguous current package document"
                )
            if identity is not None:
                validate_runtime_config(normalize_runtime_config(parsed_legacy, identity))
            operation = "legacy-conversion"
        elif canonical_exists:
            if identity is None:
                return _failure("config-fix", "Canonical metadata requires a valid version directory")
            original = destination.read_text(encoding="utf-8")
            rendered, _ = sync_config_metadata_text(original, identity)
            imported = []
            shortcuts_dir = directory / "_shortcuts"
            if args.import_shortcuts == "true" and shortcuts_dir.is_dir():
                from .shortcuts_to_gupkg_toml import (
                    archive_imported_shortcuts,
                    read_shortcut_directory,
                    replace_shortcut_tables,
                    package_path_context,
                )

                imported = read_shortcut_directory(
                    shortcuts_dir, package_path_context(directory)
                )
                if imported:
                    rendered = replace_shortcut_tables(rendered, imported)
            operation = "synchronize"
        else:
            if identity is None:
                return _failure("config-fix", "A starter configuration requires a valid version directory")
            from .metadata import create_starter_config

            rendered = create_starter_config(identity)
            operation = "starter"
            imported = []
        # Validate the complete replacement before any backup, write, or archive.
        parsed = tomllib.loads(rendered)
        if not isinstance(parsed, dict):
            raise ConfigValidationError("config-fix produced a non-table TOML document")
        # Validate the complete replacement, including preserved unrelated
        # fields, before creating a backup or touching the destination.
        if identity is not None:
            validate_runtime_config(normalize_runtime_config(parsed, identity))
        else:
            required = {"name", "version", "localVersion", "only_portable"}
            if not required.issubset(parsed):
                raise ConfigValidationError(
                    "config-fix produced incomplete package metadata"
                )
            if not isinstance(parsed["name"], str) or not isinstance(parsed["version"], str):
                raise ConfigValidationError("config-fix produced invalid package identity")
            if not isinstance(parsed["localVersion"], int) or isinstance(parsed["localVersion"], bool):
                raise ConfigValidationError("config-fix produced invalid localVersion")
            if not isinstance(parsed["only_portable"], bool):
                raise ConfigValidationError("config-fix produced invalid only_portable")
        previous = destination.read_text(encoding="utf-8") if destination.exists() else None
        if previous == rendered:
            return _result(
                "config-fix",
                ActionResult(
                    ok=True,
                    changed=False,
                    warnings=legacy_warnings,
                    status="unchanged",
                ),
                data={"config": {"path": _normal_path(destination), "operation": operation}},
            )
        backup_path = _atomic_config_replacement(destination, rendered, backup=args.backup)
        if canonical_exists and args.import_shortcuts == "true" and imported:
            from .shortcuts_to_gupkg_toml import archive_imported_shortcuts

            archive_imported_shortcuts(directory / "_shortcuts")
        result = ActionResult(
            ok=True,
            changed=True,
            warnings=legacy_warnings,
            status="fixed",
        )
        return _result(
            "config-fix",
            result,
            data={
                "config": {
                    "path": _normal_path(destination),
                    "operation": operation,
                    "backup_path": _normal_path(backup_path) if backup_path else None,
                }
            },
        )
    except (ConfigValidationError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        return _failure("config-fix", str(exc))
    except OSError as exc:
        return _failure("config-fix", str(exc), EXIT_MUTATION_ERROR)


def _target_record(target: Any, result: ActionResult | None = None) -> dict[str, Any]:
    """Serialize one manager target for human and TOML renderers."""
    return {
        "id": target.target_id,
        "selector": target.package.selector,
        "scope": scope_name(target.scope).casefold(),
        "installation": target.installation_status,
        "installed_version": target.installed_version,
        "health": target.health_status,
        "status": target.update_status if result is None else (result.status or ("ok" if result.ok else "failed")),
        "changed": False if result is None else result.changed,
        "exit_code": 0 if result is None else result.exit_code,
        "warnings": [] if result is None else result.warnings,
        "errors": target.diagnostics if result is None else result.errors,
    }


def _self_report_data(report: dict[str, Any], *, operation: str | None = None) -> dict[str, Any]:
    """Serialize standalone runtime and shim diagnostics for both renderers."""
    self_data: dict[str, Any] = {}
    if operation is not None:
        self_data["operation"] = operation
    for key in ("version_root", "runtime"):
        value = report.get(key)
        if value is not None:
            self_data[key] = _normal_path(Path(value))
    if "runtime_healthy" in report:
        self_data["runtime_healthy"] = bool(report["runtime_healthy"])

    shims: list[dict[str, Any]] = []
    for shim in report.get("shims", []):
        scope = getattr(shim, "scope", None)
        path = getattr(shim, "path", None)
        config_path = getattr(shim, "config_path", None)
        shims.append(
            {
                "scope": scope_name(scope).casefold() if scope is not None else None,
                "path": _normal_path(Path(path)) if path is not None else None,
                "config_path": _normal_path(Path(config_path)) if config_path is not None else None,
                "target": getattr(shim, "target", None),
                "healthy": bool(getattr(shim, "healthy", False)),
                "diagnostic": getattr(shim, "diagnostic", None),
            }
        )
    shims.sort(key=lambda item: (item["scope"] or "", item["path"] or ""))
    return {"self": self_data, "self_shims": shims}


def _selected_scopes(value: str) -> set[Scope]:
    """Resolve aggregate manager scope, where auto means both roots."""
    if value == "auto":
        return {Scope.USER, Scope.MACHINE}
    return {Scope.USER if value == "user" else Scope.MACHINE}


def _manager_list(args: argparse.Namespace, inventory: Any) -> _Outcome:
    """Render filtered manager inventory without contacting update providers."""
    selected = [target for target in inventory.targets if target.scope in _selected_scopes(args.scope)]
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
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in _selected_scopes(args.scope))
    if args.manager_command == "doctor":
        complete = complete and all(target["health"] == "healthy" for target in targets)
    result = ActionResult(ok=complete, exit_code=EXIT_SUCCESS if complete else EXIT_USER_ERROR, status="listed")
    return _result(
        "manager.list" if args.manager_command == "list" else "manager.doctor",
        result,
        data={
            "manager": {"complete": complete, "target_count": len(targets)},
            "targets": targets,
        },
    )


def _check_target(target: Any, *, allow_dependencies: bool = False) -> ActionResult:
    """Check one manager target while keeping domain progress out of reports."""
    return manager_update_target(target, allow_dependencies=allow_dependencies)


def _manager_update(args: argparse.Namespace, manager: ManagerConfig, inventory: Any, scope_value: str, allow_dependencies: bool) -> _Outcome:
    """Plan and execute manager updates while retaining every target result.

    The manager domain owns eligibility, revalidation, and per-target failure
    handling.  This wrapper only translates those structured entries into the
    CLI result records and applies the one interactive confirmation required by
    a full update.
    """
    scopes = _selected_scopes(scope_value)
    plan = plan_upgrade_all(
        inventory,
        scopes,
        lambda target: _check_target(target, allow_dependencies=allow_dependencies),
    )

    # Full updates are the only manager mode that may ask for confirmation;
    # checks and staging remain suitable for automation and never prompt.
    confirmation_error: str | None = None
    if not args.check_only and not args.download_only:
        eligible = any(entry.outcome == "eligible" for entry in plan.entries)
        if eligible and not args.yes:
            if args.format == "toml":
                confirmation_error = "manager update requires --yes when --format toml is selected"
            elif not sys.stdin.isatty():
                confirmation_error = "manager update requires --yes in non-interactive mode"
            elif input("Run planned updates? [y/N] ").strip().casefold() not in {"y", "yes"}:
                confirmation_error = "Update cancelled"
        if confirmation_error is not None:
            for entry in plan.entries:
                if entry.outcome == "eligible":
                    # The planner stores the successful check result on an
                    # eligible entry. Replace it before reporting cancellation
                    # so that confirmation failure cannot look successful.
                    entry.result = None
                    entry.outcome = "failed"
                    entry.reason = confirmation_error

    if confirmation_error is None and not args.check_only:
        def revalidate(target: Any) -> str | None:
            root = manager.system_root if target.scope == Scope.MACHINE else manager.user_root
            return manager_revalidate_target(target, root, quiet=True)

        action = (
            manager_download_target
            if args.download_only
            else manager_upgrade_target
        )

        def execute(target: Any) -> ActionResult:
            if args.download_only:
                return action(
                    target,
                    no_checksum=args.no_checksum,
                    allow_dependencies=allow_dependencies,
                )
            return action(
                target,
                no_checksum=args.no_checksum,
                allow_dependencies=allow_dependencies,
                shim_linkage=args.shim_linkage or manager.shim_linkage,
            )

        execute_upgrade_plan(plan, revalidate, execute)

    target_results: list[dict[str, Any]] = []
    highest = EXIT_SUCCESS
    for entry in plan.entries:
        result = entry.result
        if result is None and entry.outcome == "failed":
            result = ActionResult(
                ok=False,
                errors=[entry.reason or "manager target failed"],
                exit_code=EXIT_USER_ERROR,
            )
        record = _target_record(entry.target, result)
        if result is None:
            record["status"] = entry.reason or entry.outcome
        if entry.target.candidate_version is not None:
            record["candidate_version"] = entry.target.candidate_version
        target_results.append(record)
        highest = max(highest, record["exit_code"])
    changed = any(record["changed"] for record in target_results)
    ok = highest == EXIT_SUCCESS
    status = "checked" if args.check_only else ("downloaded" if args.download_only else "updated")
    return _result(
        "manager.update",
        ActionResult(ok=ok, changed=changed, exit_code=highest, status=status),
        data={
            "manager": {"target_count": len(target_results), "scope": scope_value},
            "targets": target_results,
            "summary": {
                "total": len(target_results),
                "changed": sum(record["changed"] for record in target_results),
                "failed": sum(record["exit_code"] != 0 for record in target_results),
            },
        },
    )


def _registry_outcome(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Run one registry cache operation and return structured data."""
    cache = manager.registry_cache
    if cache is None:
        return _failure("manager.registry", "Manager configuration has no registry cache")
    if args.manager_command == "registry" and args.registry_command == "sync":
        result = _invoke("manager.registry.sync", lambda: sync_registry(cache)).result
        return _result("manager.registry.sync", result, data={"registry": {"cache": _normal_path(cache)}})
    state = registry_status(cache)
    if args.manager_command == "registry":
        return _result(
            "manager.registry.status",
            ActionResult(True, status="current"),
            data={"registry": {"cache": _normal_path(cache), "revision": state.revision, "source": state.source}},
        )
    if state.tree_path is None and not args.offline:
        sync_result = _invoke("manager.search", lambda: sync_registry(cache)).result
        if not sync_result.ok:
            return _result("manager.search", sync_result)
        # Read the committed state again so the result describes the cache
        # that the search actually used, including its new revision.
        state = registry_status(cache)
    try:
        matches = search_registry(cache, args.query or "")
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return _failure("manager.search", str(exc))
    records = [{"selector": item.selector, "path": _normal_path(item.version_path)} for item in matches]
    return _result(
        "manager.search",
        ActionResult(True, status="searched"),
        data={"registry": {"cache": _normal_path(cache), "revision": state.revision}, "registry_packages": records},
    )


def _manager_install(args: argparse.Namespace, manager: ManagerConfig) -> _Outcome:
    """Resolve an exact registry selector and delegate to package installation."""
    if args.scope == "auto":
        return _failure("manager.install", "manager install requires --scope user or --scope system")
    cache = manager.registry_cache
    if cache is None:
        return _failure("manager.install", "Manager configuration has no registry cache")
    if (
        any(part in args.selector for part in ("/", "\\"))
        or Path(args.selector).is_absolute()
        or bool(Path(args.selector).drive)
        or args.selector in {".", "..", "~"}
    ):
        return _failure("manager.install", "Registry selectors cannot be paths")
    try:
        state = registry_status(cache)
        if state.tree_path is None and args.offline:
            return _failure("manager.install", "--offline requires a usable cached registry tree")
        if state.tree_path is None:
            sync_result = _invoke("manager.install", lambda: sync_registry(cache)).result
            if not sync_result.ok:
                return _result("manager.install", sync_result)
        package = resolve_selector(cache, args.selector)
        scope = _scope(args.scope)
        root = manager.user_root if scope == Scope.USER else manager.system_root
        target = root / package.selector / package.version_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(package.version_path, target)
        result = _invoke(
            "manager.install",
            lambda: package_workflows.install_package(
                target,
                scope=scope,
                install_context=installation_context(manager, scope),
                local_deps_autoinstall=args.allow_hook_dependency_install,
                shim_linkage=manager.shim_linkage,
            ),
        )
        result.data["manager"] = {"selector": package.selector, "scope": args.scope, "path": _normal_path(target)}
        return result
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return _failure("manager.install", str(exc))
    except OSError as exc:
        return _failure("manager.install", str(exc), EXIT_MUTATION_ERROR)


def _manager_command(args: argparse.Namespace) -> _Outcome:
    """Resolve manager configuration and dispatch its explicit subcommand."""
    if args.manager_command is None:
        return _failure("manager", "manager requires a subcommand")
    if args.max_depth < 1:
        return _failure("manager", "--max-depth must be at least 1")

    # Registry and self workflows use only their own configured state.  Avoid
    # traversing package roots for those commands so a missing or inaccessible
    # root cannot hide a usable registry or standalone diagnostic.
    manager, error = _manager_config(args)
    if error is not None:
        return error
    assert manager is not None
    inventory = None
    if args.manager_command in {"tui", "list", "doctor", "update"}:
        try:
            inventory = discover_manager(manager, max_depth=args.max_depth)
        except (OSError, ValueError, ConfigValidationError) as exc:
            return _failure("manager", str(exc))
    if args.manager_command == "tui":
        from .dependencies import ensure_runtime_dependencies
        from .manager_tui import run_manager_tui

        assert inventory is not None
        ensure_runtime_dependencies("tui")
        code = run_manager_tui(manager, inventory)
        return _result(
            "manager.tui",
            ActionResult(code == EXIT_SUCCESS, exit_code=code),
            data={"manager": {"interactive": True}},
        )
    if args.manager_command in {"list", "doctor"}:
        assert inventory is not None
        return _manager_list(args, inventory)
    if args.manager_command == "update":
        assert inventory is not None
        return _manager_update(args, manager, inventory, args.scope, args.allow_hook_dependency_install)
    if args.manager_command == "install":
        return _manager_install(args, manager)
    if args.manager_command in {"registry", "search"}:
        return _registry_outcome(args, manager)
    if args.manager_command == "self":
        if args.self_command == "status":
            from .distribution import self_status

            try:
                report = self_status()
            except (OSError, ConfigValidationError, ValueError) as exc:
                return _failure("manager.self.status", str(exc), EXIT_MUTATION_ERROR)
            return _result(
                "manager.self.status",
                ActionResult(report["runtime_healthy"], status="healthy" if report["runtime_healthy"] else "unhealthy", exit_code=0 if report["runtime_healthy"] else EXIT_MUTATION_ERROR),
                data=_self_report_data(report),
            )
        from .distribution import repair_self

        scope = _scope(args.scope if args.scope != "auto" else "user")
        result = _invoke(
            f"manager.self.{args.self_command}",
            lambda: repair_self(
                scope=scope,
                install_context=installation_context(manager, scope, shim_linkage=args.shim_linkage),
            ),
        )
        try:
            from .distribution import self_status

            report = self_status()
            data = _self_report_data(report, operation=args.self_command)
        except (OSError, ConfigValidationError, ValueError):
            data = {"self": {"operation": args.self_command}}
        return _result(result.command, result.result, data=data, captured=result.captured)
    return _failure("manager", f"Unsupported manager command: {args.manager_command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one parsed gupkg invocation and return its process status."""
    parser, manager_parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "manager" and args.manager_command is None:
        manager_parser.print_help()
        return EXIT_USER_ERROR

    if args.command == "manager" and args.manager_command == "tui":
        # Interactive commands own their terminal, so global rendering and
        # pause behavior must never add text or wait around the TUI.
        try:
            return _manager_command(args).result.exit_code
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            return _dispatch_failure("manager.tui", exc).result.exit_code
    if args.command == "manager":
        try:
            outcome = _manager_command(args)
        except Exception as exc:  # pragma: no cover - defensive process boundary
            outcome = _dispatch_failure("manager", exc)
    elif args.command == "config-fix":
        outcome = _config_fix(args)
    elif args.command == "tui":
        package_path, error = _resolve_package_path(args.path)
        if error is not None:
            error.command = args.command
            outcome = error
        else:
            from .dependencies import ensure_runtime_dependencies
            from .tui import run_tui

            ensure_runtime_dependencies("tui")
            return run_tui(str(package_path))
    else:
        package_path, error = _resolve_package_path(args.path)
        if error is not None:
            error.command = args.command
            outcome = error
        elif args.command == "install":
            scope = _scope(args.scope)
            outcome = _invoke(
                "install",
                lambda: package_workflows.install_package(
                    package_path,
                    scope=scope,
                    allow_downgrade=args.allow_downgrade,
                    refresh_app=args.refresh_app,
                    no_checksum=args.no_checksum,
                    local_deps_autoinstall=args.allow_hook_dependency_install,
                    shim_linkage=args.shim_linkage,
                ),
            )
            outcome.data["package"] = _package_data(package_path, scope)
        elif args.command == "update":
            scope = _scope(args.scope)
            if args.check_only:
                outcome = _invoke("update", lambda: package_workflows.check_package_update(package_path, local_deps_autoinstall=args.allow_hook_dependency_install))
            elif args.download_only:
                outcome = _invoke("update", lambda: package_workflows.download_package_update(package_path, no_checksum=args.no_checksum, local_deps_autoinstall=args.allow_hook_dependency_install))
            else:
                outcome = _invoke("update", lambda: package_workflows.full_package_upgrade(package_path, scope=scope, no_checksum=args.no_checksum, local_deps_autoinstall=args.allow_hook_dependency_install, shim_linkage=args.shim_linkage))
            outcome.data["package"] = _package_data(package_path, scope)
        elif args.command == "config-check":
            scope = _scope(args.scope)
            outcome = _invoke("config-check", lambda: package_workflows.health_check_package(package_path, scope=scope))
            outcome.data["config"] = {"path": _normal_path(package_path), "operation": "check"}
        else:
            outcome = _failure("package", f"Unsupported command: {args.command}")

    if args.format == "toml" and args.command != "tui":
        _render_toml(outcome)
    else:
        _render_human(outcome)
    if args.pause and args.command != "tui":
        print("Press any key to continue...", file=sys.stderr)
        wait_for_keypress()
    return outcome.result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
