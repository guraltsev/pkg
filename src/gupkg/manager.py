"""Load fixed-location manager configuration, build inventories, and run batches.

The manager domain validates one small TOML schema, expands paths without a
shell, and delegates package traversal to :func:`discover_collection`. Loading
configuration and inventory is read-only and never creates a configured root.
Batch updates are planned without downloading or activating anything, then
executed target by target with revalidation and per-target failure isolation.

Usage and API
-------------
Call ``discover_manager_config(...)`` and ``load_manager_config(...)``, then
``discover_manager(...)`` for a scoped inventory. ``plan_upgrade_all(...)`` and
``execute_upgrade_plan(...)`` form the batch-update boundary shared by the CLI
and the manager TUI; ``manager_update_target(...)``,
``manager_revalidate_target(...)``, ``manager_upgrade_target(...)``, and
``manager_download_target(...)`` are the per-target operations they use.

Implementation Approach
-----------------------
Every target is a package discovered below exactly one configured root, and
that root fixes its scope. Per-target operations call the ordinary package
workflows with the manager's install context and capture their progress
output, so callers own presentation.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from .collection import DiscoveredPackage, Inventory, discover_collection
from .configuration import read_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_USER_ERROR,
    PackageIdentity,
    Scope,
    compare_package_versions,
    read_toml_file,
)
from .layout import compute_scope_paths, inspect_current
from .registry import OFFICIAL_SOURCE


@dataclass(frozen=True)
class ManagerConfig:
    """Describe validated manager configuration and its scoped destinations."""

    path: Path
    system_root: Path
    user_root: Path
    system_bin: Path | None = None
    user_bin: Path | None = None
    registry_cache: Path = field(default_factory=lambda: default_registry_cache())
    registry_source: str = OFFICIAL_SOURCE
    shim_linkage: str = "dynamic"
    schema_version: int = 2

    def root(self, scope: Scope) -> Path:
        """Return the configured package root that owns *scope*."""
        return self.user_root if scope == Scope.USER else self.system_root


@dataclass(frozen=True)
class InstallationContext:
    """Carry manager-owned destinations into one package installation."""

    scope: Scope
    collection_root: Path
    bin_dir: Path
    manager_config: Path
    shortcut_root: Path | None = None
    registry_cache: Path | None = None
    shim_linkage: str = "dynamic"

    def as_scope_paths(self) -> dict[str, Path | str]:
        """Return the component scope-path mapping with manager paths applied."""
        paths: dict[str, Path | str] = {
            "bin_dir": self.bin_dir,
            "collection_root": self.collection_root,
            "shim_linkage": self.shim_linkage,
        }
        if self.shortcut_root is not None:
            paths["shortcut_root"] = self.shortcut_root
        return paths


@dataclass
class ManagedTarget:
    """Describe one scoped package available in a manager root."""

    target_id: str
    scope: Scope
    package: DiscoveredPackage
    installation_status: str
    installed_version: str | None
    local_version: str | None
    health_status: str
    update_status: str = "unchecked"
    candidate_version: str | None = None
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class ManagedScope:
    """Describe discovery for one configured scope, including incomplete roots."""

    scope: Scope
    root: Path
    inventory: Inventory | None
    complete: bool
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class ManagerInventory:
    """Contain deterministic scoped targets and root-level diagnostics."""

    config: ManagerConfig
    scopes: list[ManagedScope]
    targets: list[ManagedTarget]


@dataclass
class UpgradePlanEntry:
    """Record one target's planned and, after execution, actual batch outcome."""

    target: ManagedTarget
    outcome: str
    reason: str | None = None
    result: ActionResult | None = None


@dataclass
class UpgradePlan:
    """Contain a deterministic, non-mutating manager upgrade plan."""

    entries: list[UpgradePlanEntry]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Windows-style ``%NAME%`` references accepted in manager paths.
_VARIABLE_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def discover_manager_config(
    explicit: Path | None = None,
    *,
    module_directory: Path | None = None,
    gupkg_home: str | None = None,
    appdata: str | None = None,
) -> Path | None:
    """Select a manager file from the fixed, non-working-directory locations.

    Parameters
    ----------
    explicit : Path, optional
        ``--config`` value; returned as-is when given.
    module_directory : Path, optional
        Directory of the installed ``gupkg`` package; defaults to this one.
    gupkg_home, appdata : str, optional
        Overrides for ``GUPKG_HOME`` and ``APPDATA``.

    Returns
    -------
    Path or None
        The first existing candidate from :func:`manager_config_candidates`.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    for candidate in manager_config_candidates(
        module_directory=module_directory, gupkg_home=gupkg_home, appdata=appdata
    ):
        if candidate.is_file():
            return candidate
    return None


def manager_config_candidates(
    *,
    module_directory: Path | None = None,
    gupkg_home: str | None = None,
    appdata: str | None = None,
) -> list[Path]:
    """Return the ordered fixed locations searched for ``gupkg-config.toml``.

    The search order is the installed package directory, ``GUPKG_HOME``, and
    ``%APPDATA%\\gupkg``; unset variables are skipped and the working directory
    is never searched.
    """
    directory = Path(module_directory) if module_directory else Path(__file__).resolve().parent
    candidates = [directory / "gupkg-config.toml"]
    home = gupkg_home if gupkg_home is not None else os.environ.get("GUPKG_HOME")
    if home:
        candidates.append(Path(home) / "gupkg-config.toml")
    roaming = appdata if appdata is not None else os.environ.get("APPDATA")
    if roaming:
        candidates.append(Path(roaming) / "gupkg" / "gupkg-config.toml")
    return candidates


def load_manager_config(path: Path) -> ManagerConfig:
    """Read and strictly validate one schema-version-two manager file.

    Parameters
    ----------
    path : Path
        Manager configuration file. Relative configured paths resolve against
        its directory.

    Returns
    -------
    ManagerConfig
        Validated configuration with absolute, expanded paths.

    Raises
    ------
    ConfigValidationError
        If the file is missing, unreadable, or violates the schema.
    """
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ConfigValidationError(f"Manager configuration is not a regular file: {path}")
    try:
        raw = read_toml_file(path)
    except Exception as exc:
        raise ConfigValidationError(f"Could not read manager configuration {path}: {exc}") from exc

    # Only the current schema is accepted; older files must be migrated.
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        raise ConfigValidationError(
            "schema_version must be the integer 2; older manager configurations "
            "must be explicitly migrated"
        )
    _exact_table_keys(raw, {"mode", "schema_version", "packages", "bin"}, "manager configuration", optional={"registry", "shims"})
    if raw["mode"] != "manager":
        raise ConfigValidationError("mode must be exactly 'manager'")

    # Package roots and bin directories come in user/system pairs that must
    # not collide; the registry cache must live outside both package roots.
    _exact_table_keys(raw["packages"], {"system", "user"}, "[packages]")
    _exact_table_keys(raw["bin"], {"system", "user"}, "[bin]")
    registry = raw.get("registry", {})
    # ``channel`` is accepted for configurations written before ``source``
    # existed; "stable" was its only value and it no longer has any effect.
    _exact_table_keys(registry, set(), "[registry]", optional={"cache", "source", "channel"})
    if registry.get("channel", "stable") != "stable":
        raise ConfigValidationError("[registry].channel is obsolete; use [registry].source instead")
    shims = raw.get("shims", {"linkage": "dynamic"})
    _exact_table_keys(shims, {"linkage"}, "[shims]")
    roots = {name: _expand_manager_path(value, path.parent, f"packages.{name}") for name, value in raw["packages"].items()}
    bins = {name: _expand_manager_path(value, path.parent, f"bin.{name}") for name, value in raw["bin"].items()}
    cache = (
        _expand_manager_path(registry["cache"], path.parent, "registry.cache")
        if "cache" in registry
        else default_registry_cache()
    )
    source = registry.get("source", OFFICIAL_SOURCE)
    if not isinstance(source, str) or urllib.parse.urlparse(source).scheme.lower() not in {"http", "https", "file"}:
        raise ConfigValidationError(
            "[registry].source must be an http(s):// or file: URL of a ZIP archive"
        )
    if _same_or_nested(roots["system"], roots["user"]):
        raise ConfigValidationError("Configured system and user roots must be distinct and non-nested")
    if bins["system"] == bins["user"]:
        raise ConfigValidationError("Configured system and user bin directories must be distinct")
    if _same_or_nested(cache, roots["system"]) or _same_or_nested(cache, roots["user"]):
        raise ConfigValidationError("Registry cache must be outside both package roots")
    if shims["linkage"] not in {"dynamic", "static"}:
        raise ConfigValidationError("[shims].linkage must be exactly 'dynamic' or 'static'")
    for field_name, value in (("bin.system", bins["system"]), ("bin.user", bins["user"]), ("registry.cache", cache)):
        if value.exists() and not value.is_dir():
            raise ConfigValidationError(f"Configured {field_name} path is not a directory: {value}")
    return ManagerConfig(
        path,
        roots["system"],
        roots["user"],
        system_bin=bins["system"],
        user_bin=bins["user"],
        registry_cache=cache,
        registry_source=source,
        shim_linkage=shims["linkage"],
    )


def manager_config_text(config: ManagerConfig) -> str:
    """Render a reviewed schema-version-two manager configuration as TOML.

    Parameters
    ----------
    config : ManagerConfig
        Configuration to render; missing bin and cache paths use the scope
        defaults.

    Returns
    -------
    str
        TOML text that :func:`load_manager_config` accepts.
    """
    user_bin = config.user_bin or compute_scope_paths(Scope.USER)["bin_dir"]
    system_bin = config.system_bin or compute_scope_paths(Scope.MACHINE)["bin_dir"]
    def toml_path(value: Path) -> str:
        """Render a Windows path as a TOML literal string."""
        return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"

    return (
        'mode = "manager"\n'
        "schema_version = 2\n\n"
        "[packages]\n"
        f"system = {toml_path(config.system_root)}\n"
        f"user = {toml_path(config.user_root)}\n\n"
        "[bin]\n"
        f"system = {toml_path(system_bin)}\n"
        f"user = {toml_path(user_bin)}\n\n"
        "[registry]\n"
        f"cache = {toml_path(config.registry_cache)}\n"
        f"source = {json.dumps(config.registry_source)}\n\n"
        "[shims]\n"
        f'linkage = "{config.shim_linkage}"\n'
    )


def default_manager_config(path: Path | None = None) -> ManagerConfig:
    """Build the reviewed manager configuration used by interactive initialization.

    Parameters
    ----------
    path : Path, optional
        Destination for the configuration file; defaults to
        ``%APPDATA%\\gupkg\\gupkg-config.toml``.

    Returns
    -------
    ManagerConfig
        Defaults for the user and system collections, executable directories,
        and registry cache.

    Raises
    ------
    ValueError
        If the Windows environment does not identify the required default
        locations.
    """
    environment = {name: os.environ.get(name) for name in ("APPDATA", "USERPROFILE", "LOCALAPPDATA", "SYSTEMDRIVE")}
    if path is None and not environment["APPDATA"]:
        raise ValueError("APPDATA is not set; cannot initialize manager mode")
    if not all(environment[name] for name in ("USERPROFILE", "LOCALAPPDATA", "SYSTEMDRIVE")):
        raise ValueError(
            "USERPROFILE, LOCALAPPDATA, and SYSTEMDRIVE are required to initialize manager mode"
        )
    system_drive = environment["SYSTEMDRIVE"]
    if len(system_drive) == 2 and system_drive[1] == ":":
        system_drive += "\\"
    user_profile = Path(environment["USERPROFILE"])
    return ManagerConfig(
        path=Path(path) if path is not None else Path(environment["APPDATA"]) / "gupkg" / "gupkg-config.toml",
        system_root=Path(system_drive) / "opt",
        user_root=user_profile / "opt",
        system_bin=Path(system_drive) / "bin",
        user_bin=user_profile / "bin",
        registry_cache=default_registry_cache(),
    )


def default_registry_cache() -> Path:
    """Return the default per-user registry cache without creating it."""
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local_app_data) / "gupkg" / "registry"


def installation_context(
    config: ManagerConfig, scope: Scope, *, shim_linkage: str | None = None
) -> InstallationContext:
    """Build the manager-owned destinations for one explicit scope.

    Parameters
    ----------
    config : ManagerConfig
        Validated manager configuration.
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``.
    shim_linkage : {"dynamic", "static"}, optional
        Overrides the configured shim linkage.

    Returns
    -------
    InstallationContext
        Package root, bin directory, and Start Menu root for *scope*.
    """
    if scope == Scope.AUTO:
        raise ValueError("Installation context requires an explicit scope")
    bin_dir = config.user_bin if scope == Scope.USER else config.system_bin
    try:
        defaults = compute_scope_paths(scope)
    except ValueError:
        defaults = {}
    if bin_dir is None:
        bin_dir = defaults["bin_dir"]
    return InstallationContext(
        scope=scope,
        collection_root=config.root(scope),
        bin_dir=bin_dir,
        manager_config=config.path,
        shortcut_root=defaults.get("shortcut_root"),
        registry_cache=config.registry_cache,
        shim_linkage=shim_linkage or config.shim_linkage,
    )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def discover_manager(config: ManagerConfig, *, max_depth: int = 8) -> ManagerInventory:
    """Discover both configured roots and return scoped, deterministic targets.

    Parameters
    ----------
    config : ManagerConfig
        Validated manager configuration.
    max_depth : int, default=8
        Maximum descent through marked grouping directories.

    Returns
    -------
    ManagerInventory
        Targets sorted by selector and then user-before-system; a missing or
        unreadable root is reported as an incomplete scope.
    """
    scopes: list[ManagedScope] = []
    targets: list[ManagedTarget] = []
    for scope in (Scope.USER, Scope.MACHINE):
        root = config.root(scope)
        try:
            if not root.is_dir():
                raise OSError("root does not exist" if not root.exists() else "root is not a directory")
            inventory = discover_collection(root, max_depth=max_depth)
        except OSError as exc:
            scopes.append(ManagedScope(
                scope, root, None, False,
                [f"Cannot access {scope_name(scope)} root {root}: {exc}"],
            ))
            continue
        scopes.append(ManagedScope(scope, root, inventory, inventory.complete, list(inventory.diagnostics)))
        targets.extend(_managed_target(scope, package, inventory.complete) for package in inventory.packages)
    targets.sort(key=lambda target: (
        target.package.selector.casefold(), target.package.selector, _scope_order(target.scope)
    ))
    return ManagerInventory(config, scopes, targets)


def _managed_target(scope: Scope, package: DiscoveredPackage, complete: bool) -> ManagedTarget:
    """Describe one discovered package's activation, versions, and health."""
    current = inspect_current(package.root)
    diagnostics = list(package.diagnostics) + list(current.diagnostics)

    # Validate every manifest through the package configuration boundary so
    # inventory health reflects real package semantics, not file presence.
    for manifest in package.manifests:
        identity = PackageIdentity.from_version_path(
            package.root, manifest.version_path, is_current=manifest.version_path == current.version_path
        )
        try:
            read_runtime_config(identity)
        except Exception as exc:
            diagnostics.append(f"Invalid manifest {manifest.path}: {exc}")
    health = "incomplete" if not complete else ("unhealthy" if diagnostics else "healthy")
    return ManagedTarget(
        f"{scope.value}:{package.selector}",
        scope,
        package,
        current.status,
        current.version_path.name if current.version_path else None,
        max(
            (manifest.version_path.name for manifest in package.manifests),
            key=cmp_to_key(compare_package_versions),
            default=None,
        ),
        health,
        diagnostics=diagnostics,
    )


def scope_name(scope: Scope) -> str:
    """Return the user-facing manager scope name, ``User`` or ``System``."""
    return "User" if scope == Scope.USER else "System"


def _scope_order(scope: Scope) -> int:
    """Sort user targets before system targets."""
    return 0 if scope == Scope.USER else 1


# ---------------------------------------------------------------------------
# Batch planning and execution
# ---------------------------------------------------------------------------


def plan_upgrade_all(
    inventory: ManagerInventory,
    scopes: set[Scope],
    check_target,
) -> UpgradePlan:
    """Plan eligible installed targets without downloading or activating them.

    Parameters
    ----------
    inventory : ManagerInventory
        Fresh manager inventory to evaluate.
    scopes : set[Scope]
        Configured scopes selected by the caller.
    check_target : callable
        Boundary that refreshes one target's ``update_status`` and returns an
        :class:`~gupkg.core.ActionResult`.

    Returns
    -------
    UpgradePlan
        User targets before system targets, each ``skipped`` (with a reason),
        ``failed`` (check failure), or ``eligible``.
    """
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    selected = sorted(
        (target for target in inventory.targets if target.scope in scopes),
        key=lambda target: (_scope_order(target.scope), target.package.selector.casefold(), target.package.selector),
    )
    entries: list[UpgradePlanEntry] = []
    for target in selected:
        # Only healthy installed targets of complete scopes are ever checked.
        skip_reason = (
            "incomplete" if not complete
            else "uninstalled" if target.installation_status == "not-installed"
            else "broken" if target.installation_status == "broken"
            else "unhealthy" if target.health_status != "healthy"
            else None
        )
        if skip_reason is not None:
            entries.append(UpgradePlanEntry(target, "skipped", skip_reason))
            continue
        result = check_target(target)
        if target.update_status in {"not-configured", "current"} and result.ok:
            entries.append(UpgradePlanEntry(target, "skipped", target.update_status, result))
        elif target.update_status == "available" and result.ok:
            entries.append(UpgradePlanEntry(target, "eligible", result=result))
        else:
            entries.append(UpgradePlanEntry(target, "failed", "failed-check", result))
    return UpgradePlan(entries)


def execute_upgrade_plan(
    plan: UpgradePlan,
    revalidate_target,
    upgrade_target,
    *,
    cancel_requested=None,
) -> UpgradePlan:
    """Execute eligible plan entries in order and retain every outcome.

    A failing target never stops the batch; every later eligible target still
    runs.

    Parameters
    ----------
    plan : UpgradePlan
        Previously generated plan; its entries are updated in place.
    revalidate_target : callable
        Returns ``None`` when a target is still safe to mutate, or an
        explanatory string when it changed after planning.
    upgrade_target : callable
        Single-package operation returning an :class:`ActionResult`.
    cancel_requested : callable, optional
        Checked before each new package operation. A true result stops
        scheduling without interrupting an operation already in progress.

    Returns
    -------
    UpgradePlan
        The same plan, with ``upgraded``, ``current``, ``failed``, or
        ``not-attempted`` outcomes and results attached.
    """
    for entry in plan.entries:
        if entry.outcome != "eligible":
            continue
        if cancel_requested is not None and cancel_requested():
            entry.outcome, entry.reason = "not-attempted", "cancelled"
            continue

        # Revalidate ownership and health immediately before mutation, and
        # keep a failure as the entry's result so the earlier successful
        # check result cannot be reported for an invalidated target.
        try:
            problem = revalidate_target(entry.target)
            failure = ActionResult(False, errors=[problem], exit_code=EXIT_USER_ERROR) if problem else None
        except Exception as exc:
            failure = _exception_result(exc)
        if failure is not None:
            entry.result = failure
            entry.outcome, entry.reason = "failed", failure.errors[0]
            continue

        # Isolate one target's operational failure so later eligible targets
        # still run and the batch retains a result for every target.
        try:
            entry.result = upgrade_target(entry.target)
        except Exception as exc:
            entry.result = _exception_result(exc)
        if not entry.result.ok:
            entry.outcome, entry.reason = "failed", "upgrade-failed"
        elif entry.result.changed or entry.result.status in {"installed-update", "downloaded"}:
            entry.outcome = "upgraded"
        else:
            entry.outcome = "current"
    return plan


def _exception_result(exc: Exception) -> ActionResult:
    """Translate an exception from one per-target boundary into a failed result."""
    if isinstance(exc, (ConfigValidationError, ValueError, FileNotFoundError)):
        code = EXIT_USER_ERROR
    elif isinstance(exc, OSError):
        code = EXIT_MUTATION_ERROR
    else:
        code = EXIT_INTERNAL_ERROR
    return ActionResult(False, errors=[str(exc)], exit_code=code)


# ---------------------------------------------------------------------------
# Per-target operations
# ---------------------------------------------------------------------------


def manager_update_target(target: ManagedTarget, *, allow_dependencies: bool = False) -> ActionResult:
    """Refresh one managed target's update status through the package workflow.

    Parameters
    ----------
    target : ManagedTarget
        Target whose active (or newest local) manifest should be checked; its
        ``update_status`` and ``candidate_version`` are updated.
    allow_dependencies : bool, default=False
        Whether trusted package-local hooks may install missing imports.

    Returns
    -------
    ActionResult
        The update-check outcome and its process status.
    """
    from .gupkg import check_package_update

    wanted = target.installed_version or target.local_version
    manifest = next(
        (item for item in target.package.manifests if item.version_path.name == wanted), None
    )
    if manifest is None:
        result = ActionResult(False, errors=["No manifest is available for the local version"], exit_code=EXIT_USER_ERROR)
    else:
        result = _quiet(lambda: check_package_update(manifest.version_path, local_deps_autoinstall=allow_dependencies))
    if result.ok and result.status in {"available", "current", "not-configured"}:
        target.update_status = result.status
    else:
        target.update_status = "error" if not result.ok else "not-configured"
    target.candidate_version = None
    if target.update_status == "available":
        from .updates import load_update_state, update_paths

        state = load_update_state(update_paths(target.package.root)["state"])
        target.candidate_version = state.get("lastCandidateVersion")
    target.diagnostics.extend(result.errors)
    return result


def manager_revalidate_target(target: ManagedTarget, config: ManagerConfig) -> str | None:
    """Recheck managed ownership and health immediately before mutation.

    Parameters
    ----------
    target : ManagedTarget
        Target whose activation and package health must still be valid.
    config : ManagerConfig
        Configuration whose scope root must still contain the target.

    Returns
    -------
    str or None
        A diagnostic when revalidation fails, otherwise ``None``.
    """
    from .gupkg import health_check_package

    try:
        root = target.package.root.resolve()
        if not root.is_relative_to(config.root(target.scope).resolve()):
            return "target path escaped configured root"
        current = inspect_current(target.package.root)
        if current.status != "installed" or current.version_path is None:
            return f"current changed to {current.status}"
        if not current.version_path.resolve().is_relative_to(root):
            return "current target escaped configured root"
        if not _quiet(lambda: health_check_package(current.version_path, scope=target.scope)).ok:
            return "health changed before upgrade"
    except (OSError, ValueError, RuntimeError) as exc:
        return str(exc)
    return None


def manager_upgrade_target(
    target: ManagedTarget,
    config: ManagerConfig,
    *,
    no_checksum: bool = False,
    allow_dependencies: bool = False,
    shim_linkage: str | None = None,
) -> ActionResult:
    """Check, stage, and activate one managed target's update.

    Parameters
    ----------
    target : ManagedTarget
        Managed package to update.
    config : ManagerConfig
        Manager configuration whose bin and package roots receive the install.
    no_checksum : bool, default=False
        Skip an applicable payload checksum.
    allow_dependencies : bool, default=False
        Permit trusted package-local hooks to install missing imports.
    shim_linkage : {"dynamic", "static"}, optional
        Native wrapper linkage; defaults to the configured linkage.

    Returns
    -------
    ActionResult
        Structured package-update outcome.
    """
    from .gupkg import full_package_upgrade

    linkage = shim_linkage or config.shim_linkage
    return _quiet(lambda: full_package_upgrade(
        target.package.root,
        scope=target.scope,
        no_checksum=no_checksum,
        local_deps_autoinstall=allow_dependencies,
        shim_linkage=linkage,
        install_context=installation_context(config, target.scope, shim_linkage=linkage),
    ))


def manager_download_target(
    target: ManagedTarget,
    *,
    no_checksum: bool = False,
    allow_dependencies: bool = False,
) -> ActionResult:
    """Stage one managed package update without activating it.

    The package workflow rechecks its source, so a staged payload is never
    inferred from stale manager inventory.

    Parameters
    ----------
    target : ManagedTarget
        Managed package whose configured update should be staged.
    no_checksum : bool, default=False
        Skip an applicable payload checksum while staging the update.
    allow_dependencies : bool, default=False
        Permit trusted package-local hooks to install missing imports.

    Returns
    -------
    ActionResult
        Structured staging outcome.
    """
    from .gupkg import download_package_update

    return _quiet(lambda: download_package_update(
        target.package.root, no_checksum=no_checksum, local_deps_autoinstall=allow_dependencies
    ))


def _quiet(operation):
    """Run a package workflow with its progress output captured and discarded."""
    with contextlib.redirect_stdout(io.StringIO()):
        return operation()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _exact_table_keys(table: object, required: set[str], context: str, *, optional: set[str] = frozenset()) -> None:
    """Require a TOML table to contain exactly the required and optional keys."""
    if not isinstance(table, dict):
        raise ConfigValidationError(f"{context} must be a table containing {', '.join(sorted(required))}")
    unknown = sorted(set(table) - required - optional)
    missing = sorted(required - set(table))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown key(s): {', '.join(unknown)}")
        if missing:
            details.append(f"missing key(s): {', '.join(missing)}")
        raise ConfigValidationError(f"Invalid {context}: " + "; ".join(details))


def _expand_manager_path(value: object, base: Path, field_name: str) -> Path:
    """Expand ``%NAME%`` and ``~`` in one configured path and make it absolute."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"[{field_name}] must be a nonempty string")
    environment = {key.casefold(): item for key, item in os.environ.items()}

    def replace(match: re.Match[str]) -> str:
        """Substitute one case-insensitive environment variable."""
        name = match.group(1)
        if name.casefold() not in environment:
            raise ConfigValidationError(f"[{field_name}] references unresolved variable %{name}%")
        return environment[name.casefold()]

    path = Path(_VARIABLE_RE.sub(replace, value.strip())).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _same_or_nested(left: Path, right: Path) -> bool:
    """Return whether two paths are equal or one contains the other."""
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
