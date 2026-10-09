"""Parse the public ``gupkg`` command line and show each command's result.

The command line is a thin shell. Argparse owns the grammar; each parsed
command is handed to one function in :mod:`gupkg.commands`, and the returned
:class:`~gupkg.outcome.Outcome` is shown with the shared formatters as human
text or as one versioned TOML document. No workflow logic lives here, so every
front end behaves identically.

Usage and API
-------------
Call ``main(...)`` from the console script or with ``python -m gupkg``. It
returns the process status: ``0`` success, ``2`` user or configuration error,
``3`` failure while changing the system, ``4`` internal error.

Implementation Approach
-----------------------
Parse once, translate the namespace into a request, call the operation, format
the outcome. TOML mode redirects progress text so standard output stays
parseable even when an operation fails.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any, Sequence

from . import commands
from ._version import __version__
from .core import (
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    ActionResult,
    ConfigValidationError,
    Scope,
)
from .outcome import Outcome, failure, format_human, format_toml
from .windows import wait_for_keypress


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def _build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Build the complete parser tree without inspecting the filesystem."""
    parser = argparse.ArgumentParser(
        prog="gupkg",
        description=(
            "Local Package Manager for Windows (gupkg).\n\n"
            "Install and update self-contained Windows applications from a "
            "pkg.toml definition: Start Menu shortcuts, environment variables, "
            "PATH entries, and command wrappers, with versioned, repairable "
            "installs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--scope",
        choices=[item.value for item in Scope],
        default="auto",
        help="where to integrate: user (per-user), system (all users, needs "
        "Administrator), or auto (system for an elevated shell, else user; "
        "default)",
    )
    parser.add_argument(
        "--format",
        choices=["human", "toml"],
        default="human",
        help="human text (default) or one machine-readable TOML document on stdout",
    )
    parser.add_argument(
        "--pause", action="store_true", help="wait for a keypress before exiting"
    )
    parser.add_argument(
        "--allow-hook-dependency-install",
        action="store_true",
        help="let a package's own update scripts pip-install missing imports "
        "(off by default; those scripts are trusted code)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def package_command(
        name: str, summary: str, help_text: str, epilog: str = ""
    ) -> argparse.ArgumentParser:
        """Add a package command: a one-line summary in the command list, full text in its own help."""
        command = commands.add_parser(
            name, help=summary, description=help_text, epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        command.add_argument(
            "path", nargs="?", type=Path,
            help="package root, version directory, or its 'current' junction "
            "(default: the current directory)",
        )
        return command

    def shim_linkage(command: argparse.ArgumentParser, default: str | None) -> None:
        """Add the native-wrapper linkage option to a command that installs wrappers."""
        command.add_argument(
            "--shim-linkage", choices=["dynamic", "static"], default=default,
            help="command wrappers: dynamic (small; shared runtime DLLs beside them) "
            "or static (self-contained)" + ("; default: dynamic" if default else "; default: manager setting"),
        )

    def update_modes(command: argparse.ArgumentParser) -> None:
        """Add the mutually exclusive limits shared by package and manager updates."""
        modes = command.add_mutually_exclusive_group()
        modes.add_argument(
            "--check-only", action="store_true",
            help="only report whether an update exists; change nothing",
        )
        modes.add_argument(
            "--download-only", action="store_true",
            help="download and stage the update as a new version, but do not "
            "activate it (a later plain 'update' activates it)",
        )
        command.add_argument(
            "--no-checksum", action="store_true",
            help="skip SHA-256 verification of downloads (prints a warning)",
        )

    install = package_command(
        "install",
        "install or repair a package",
        "Activate a package version and apply its pkg.toml: download App if "
        "missing, then create shortcuts, environment variables, PATH entries, "
        "and wrappers. Safe to rerun: it repairs anything that drifted.",
        "examples:\n  gupkg install C:\\opt\\ripgrep\n  gupkg --scope user install .\n  gupkg install C:\\opt\\ripgrep --refresh-app",
    )
    install.add_argument(
        "--allow-downgrade", action="store_true",
        help="allow 'current' to move back to an older version",
    )
    install.add_argument(
        "--refresh-app", action="store_true",
        help="re-download App from [origin] even if it already has files",
    )
    install.add_argument(
        "--no-checksum", action="store_true",
        help="skip SHA-256 verification of the origin download (prints a warning)",
    )
    shim_linkage(install, "dynamic")

    update = package_command(
        "update",
        "check for, download, and activate a newer release",
        "Check for a newer release, download it into a new version directory, "
        "and activate it. Use --check-only or --download-only to stop earlier.",
        "examples:\n  gupkg update C:\\opt\\ripgrep --check-only\n  gupkg update C:\\opt\\ripgrep",
    )
    update_modes(update)
    shim_linkage(update, "dynamic")

    package_command(
        "config-check",
        "validate pkg.toml without changing anything",
        "Validate pkg.toml, directory-derived metadata, and hook references "
        "without changing anything. Reports every problem at once.",
    )

    fix = package_command(
        "config-fix",
        "create, repair, or convert pkg.toml",
        "Create a starter pkg.toml, re-sync name/version/localVersion with the "
        "directory name, import .lnk files from _shortcuts, or convert legacy "
        "JSON metadata. The old file is backed up first.",
        "examples:\n  gupkg config-fix C:\\opt\\ripgrep\\v14.1.0\n  gupkg config-fix C:\\old\\tool --output C:\\old\\tool\\pkg.toml",
    )
    backup = fix.add_mutually_exclusive_group()
    backup.add_argument(
        "--no-backup", dest="backup", action="store_false",
        help="do not keep a timestamped pkg.toml.bak.* copy",
    )
    backup.add_argument("--backup=false", dest="backup", action="store_false", help=argparse.SUPPRESS)
    fix.set_defaults(backup=True)
    fix.add_argument(
        "--import-shortcuts", choices=["true", "false"], default="true",
        help="import .lnk files from _shortcuts into [[shortcut]] (default: true)",
    )
    fix.add_argument(
        "--output", type=Path, help="destination pkg.toml (legacy conversion only)"
    )

    package_command("tui", "interactive menu for one package", "Open the interactive menu for one package.")

    manager = commands.add_parser(
        "manager",
        help="work on every package under your configured roots at once",
        description=(
            "Manage all packages under the user and system roots named in "
            "gupkg-config.toml: inventory, health checks, bulk updates, and "
            "installs from the official registry."
        ),
        epilog=MANAGER_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    manager.add_argument(
        "--config", type=Path,
        help="manager configuration file (default: search next to gupkg, "
        "%%GUPKG_HOME%%, then %%APPDATA%%\\gupkg)",
    )
    manager.add_argument(
        "--max-depth", type=int, default=8,
        help="how deep to look through grouping folders (default: 8)",
    )
    manager_commands = manager.add_subparsers(dest="manager_command", metavar="<subcommand>")
    init = manager_commands.add_parser(
        "init",
        help="create a manager configuration with sensible defaults",
        description="Write gupkg-config.toml with default package, bin, and registry "
        "locations (see the file for every setting) and create the folders it names.",
    )
    init.add_argument("--force", action="store_true", help="replace an existing configuration")
    manager_commands.add_parser("tui", help="open the interactive manager")
    listed = manager_commands.add_parser(
        "list", help="show every package, its version, health, and update state"
    )
    listed.add_argument(
        "--filter",
        choices=["all", "installed", "uninstalled", "updatable", "unhealthy"],
        default="all",
        help="show only matching packages (default: all). 'list' reads only the local disk; use 'manager update --check-only' to find new releases",
    )
    manager_commands.add_parser(
        "doctor", help="validate configuration and every package; exit 2 on any problem"
    )
    manager_update = manager_commands.add_parser(
        "update",
        help="update every installed package that has a newer release",
        description="Check all installed packages, show the plan, and (after "
        "confirmation) update them. A failing package never stops the rest.",
    )
    update_modes(manager_update)
    manager_update.add_argument(
        "--yes", action="store_true",
        help="do not ask for confirmation (required for unattended or --format toml runs)",
    )
    shim_linkage(manager_update, None)
    manager_install = manager_commands.add_parser(
        "install",
        help="install a package from the official registry by exact name",
        description="Install a package from the registry cache. Requires "
        "--scope user or --scope system placed before 'manager'.",
    )
    manager_install.add_argument("selector", help="exact registry package name, e.g. vscode")
    manager_install.add_argument(
        "--offline", action="store_true", help="use only the cached registry; never touch the network"
    )
    registry = manager_commands.add_parser("registry", help="refresh or inspect the registry cache")
    registry_commands = registry.add_subparsers(dest="registry_command", required=True, metavar="<action>")
    registry_commands.add_parser("sync", help="download the latest stable registry")
    registry_commands.add_parser("status", help="show the cached registry revision")
    search = manager_commands.add_parser("search", help="find registry packages by name")
    search.add_argument("query", nargs="?", help="substring to match (default: list everything)")
    search.add_argument(
        "--offline", action="store_true", help="use only the cached registry; never touch the network"
    )
    self_parser = manager_commands.add_parser("self", help="check or repair gupkg's own installation")
    self_commands = self_parser.add_subparsers(dest="self_command", required=True, metavar="<action>")
    self_commands.add_parser("status", help="report runtime and command-shim health")
    for name, text in (("repair", "reinstall gupkg's shims and PATH entries"), ("update", "same as repair")):
        shim_linkage(self_commands.add_parser(name, help=text), None)
    return parser, manager


# Copy-paste examples shown at the bottom of ``gupkg --help``.
EXAMPLES = """\
common tasks:
  gupkg install C:\\opt\\ripgrep              install (or repair) a package
  gupkg update C:\\opt\\ripgrep --check-only  is a newer release available?
  gupkg update C:\\opt\\ripgrep               update to the newest release
  gupkg config-check .                   validate pkg.toml in this folder
  gupkg config-fix .                     create or repair pkg.toml
  gupkg manager list                     every managed package at a glance
  gupkg manager update                   update everything, with confirmation
  gupkg manager doctor                   health check for scripts and CI

A package path may be a version folder (v1.2.3), the package folder, or its
'current' junction; with no path the current folder is used.
Exit status: 0 ok, 2 user/configuration error, 3 failure while changing the
system, 4 internal error. Add '--format toml' for scripting.
Run 'gupkg <command> --help' for the options of one command."""

MANAGER_EXAMPLES = """\
first time:
  gupkg manager init                       create gupkg-config.toml and the folders
  gupkg manager registry sync              fetch the package catalogue

examples:
  gupkg manager list
  gupkg manager update --check-only
  gupkg manager update --yes              unattended
  gupkg --scope user manager install vscode
  gupkg manager search editor
  gupkg --format toml manager doctor       machine-readable"""


# ---------------------------------------------------------------------------
# Dispatch: collect arguments, call gupkg.commands, display the outcome
# ---------------------------------------------------------------------------


def _dispatch(args: argparse.Namespace) -> Outcome:
    """Turn parsed arguments into one operation from :mod:`gupkg.commands`."""
    if args.command == "manager":
        return _dispatch_manager(args)
    if args.command == "tui":
        from .dependencies import ensure_runtime_dependencies
        from .tui import run_tui

        directory, _, error = commands.resolve_package(args.path, command="tui")
        if error is not None:
            return error
        ensure_runtime_dependencies("tui")
        code = run_tui(str(directory))
        return _interactive("tui", code)

    request = commands.PackageRequest(
        command=args.command,
        path=args.path,
        scope=Scope(args.scope),
        check_only=getattr(args, "check_only", False),
        download_only=getattr(args, "download_only", False),
        allow_downgrade=getattr(args, "allow_downgrade", False),
        refresh_app=getattr(args, "refresh_app", False),
        no_checksum=getattr(args, "no_checksum", False),
        allow_hook_dependency_install=args.allow_hook_dependency_install,
        shim_linkage=getattr(args, "shim_linkage", "dynamic"),
        backup=getattr(args, "backup", True),
        import_shortcuts=getattr(args, "import_shortcuts", "true") == "true",
        output=getattr(args, "output", None),
    )
    return commands.run_package_command(request, output=_progress_stream(args))


def _dispatch_manager(args: argparse.Namespace) -> Outcome:
    """Load the manager configuration once and run one manager subcommand."""
    sub = args.manager_command
    if args.max_depth < 1:
        return failure("manager", "--max-depth must be at least 1")
    if sub == "init":
        return commands.manager_init_outcome(args.config, force=args.force)
    try:
        config = commands.load_manager(args.config)
    except (ConfigValidationError, OSError, ValueError) as exc:
        return failure("manager", str(exc))
    progress = _progress_stream(args)
    scope = Scope(args.scope)

    # Registry and self workflows use only their own configured state, so a
    # missing or inaccessible package root cannot hide them.
    if sub == "install":
        return commands.install_registry_package(
            config, args.selector, scope, offline=args.offline,
            allow_hook_dependency_install=args.allow_hook_dependency_install, output=progress,
        )
    if sub == "registry":
        if args.registry_command == "sync":
            return commands.registry_sync_outcome(config, progress)
        return commands.registry_status_outcome(config)
    if sub == "search":
        return commands.registry_search_outcome(config, args.query or "", offline=args.offline, output=progress)
    if sub == "self":
        return commands.self_outcome(
            config, args.self_command, scope=scope, shim_linkage=getattr(args, "shim_linkage", None), output=progress
        )

    try:
        inventory = commands.discover_manager(config, max_depth=args.max_depth)
    except (OSError, ValueError, ConfigValidationError) as exc:
        return failure("manager", str(exc))
    if sub == "tui":
        from .dependencies import ensure_runtime_dependencies
        from .manager_tui import run_manager_tui

        ensure_runtime_dependencies("tui")
        return _interactive("manager.tui", run_manager_tui(config, inventory))
    if sub == "update":
        return _manager_update(args, config, inventory)
    return commands.list_outcome(
        inventory, args.scope, doctor=sub == "doctor", filter_name=getattr(args, "filter", "all")
    )


def _manager_update(args: argparse.Namespace, config: Any, inventory: Any) -> Outcome:
    """Plan, confirm (full updates only), execute, and report a bulk update."""
    hooks = args.allow_hook_dependency_install
    plan = commands.plan_updates(inventory, args.scope, allow_hook_dependency_install=hooks)
    mode = "checked" if args.check_only else ("downloaded" if args.download_only else "updated")

    # Only a full update may ask for confirmation; checks and staging remain
    # suitable for automation and never prompt.
    problem: str | None = None
    if mode == "updated" and not args.yes and commands.has_eligible_updates(plan):
        if args.format == "toml":
            problem = "manager update requires --yes when --format toml is selected"
        elif not sys.stdin.isatty():
            problem = "manager update requires --yes in non-interactive mode"
        elif input("Run planned updates? [y/N] ").strip().casefold() not in {"y", "yes"}:
            problem = "Update cancelled"
    if problem is not None:
        commands.decline_updates(plan, problem)
    elif mode != "checked":
        commands.execute_updates(
            plan,
            config,
            download_only=args.download_only,
            no_checksum=args.no_checksum,
            allow_hook_dependency_install=hooks,
            shim_linkage=args.shim_linkage,
        )
    return commands.update_outcome(plan, mode=mode, scope=args.scope)


def _interactive(command: str, code: int) -> Outcome:
    """Wrap a TUI's exit status; TUIs own the terminal, so nothing is rendered."""
    return Outcome(command, ActionResult(code == EXIT_SUCCESS, exit_code=code), {"interactive": True})


def _progress_stream(args: argparse.Namespace) -> io.StringIO | None:
    """Discard progress text in TOML mode so stdout is one parseable document."""
    return io.StringIO() if args.format == "toml" else None


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

    try:
        outcome = _dispatch(args)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        outcome = failure(args.command, f"Unexpected internal error: {exc}", EXIT_INTERNAL_ERROR)
    if outcome.data.get("interactive"):
        return outcome.result.exit_code

    if args.format == "toml":
        print(format_toml(outcome), end="")
    else:
        text, problems = format_human(outcome)
        print(text, end="")
        print(problems, end="", file=sys.stderr)
    if args.pause:
        print("Press any key to continue...", file=sys.stderr)
        wait_for_keypress()
    return outcome.result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
