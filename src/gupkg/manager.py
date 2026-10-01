"""Load fixed-location manager configuration and build scoped inventories.

The manager domain validates one small TOML schema, expands paths without a
shell, and delegates package traversal to :func:`discover_collection`. It is
read-only: loading configuration and inventory never creates or changes a
configured root.

Usage and API
-------------
Call ``load_manager_config(...)`` and ``discover_manager(...)`` for manager
workflows, then use ``select_target(...)`` to resolve a full target ID or a
unique selector.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from .collection import DiscoveredPackage, Inventory, discover_collection
from .configuration import read_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    compare_package_versions,
    read_toml_file,
    write_text_atomic,
)
from .layout import inspect_current


@dataclass(frozen=True)
class ManagerConfig:
    """Describe validated manager configuration and its scoped destinations."""

    path: Path
    system_root: Path
    user_root: Path
    system_bin: Path | None = None
    user_bin: Path | None = None
    registry_cache: Path | None = None
    channel: str = "stable"
    shim_linkage: str = "dynamic"
    schema_version: int = 2


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
        """Return the legacy component mapping with manager paths applied."""
        paths = {"bin_dir": self.bin_dir, "collection_root": self.collection_root}
        if self.shortcut_root is not None:
            paths["shortcut_root"] = self.shortcut_root
        paths["shim_linkage"] = self.shim_linkage
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
    """Record one target's planned batch outcome."""

    target: ManagedTarget
    outcome: str
    reason: str | None = None
    result: ActionResult | None = None


@dataclass
class UpgradePlan:
    """Contain a deterministic, non-mutating manager upgrade plan."""

    entries: list[UpgradePlanEntry]


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
        Boundary that refreshes one target's update status and returns an
        :class:`~gupkg.core.ActionResult`.

    Returns
    -------
    UpgradePlan
        Ordered entries describing skips, failed checks, and eligible targets.
    """
    entries: list[UpgradePlanEntry] = []
    selected_scopes = [scope for scope in inventory.scopes if scope.scope in scopes]
    complete = all(scope.complete for scope in selected_scopes)
    for target in _targets_in_upgrade_order(inventory, scopes):
        if not complete:
            entries.append(UpgradePlanEntry(target, "skipped", "incomplete"))
            continue
        if target.installation_status == "not-installed":
            entries.append(UpgradePlanEntry(target, "skipped", "uninstalled"))
            continue
        if target.installation_status == "broken":
            entries.append(UpgradePlanEntry(target, "skipped", "broken"))
            continue
        if target.health_status != "healthy":
            entries.append(UpgradePlanEntry(target, "skipped", "unhealthy"))
            continue
        result = check_target(target)
        if target.update_status == "not-configured":
            entries.append(UpgradePlanEntry(target, "skipped", "not-configured", result))
        elif not result.ok or target.update_status == "error":
            entries.append(UpgradePlanEntry(target, "failed", "failed-check", result))
        elif target.update_status == "current":
            entries.append(UpgradePlanEntry(target, "skipped", "current", result))
        elif target.update_status == "available":
            entries.append(UpgradePlanEntry(target, "eligible", result=result))
        else:
            entries.append(UpgradePlanEntry(target, "failed", "failed-check", result))
    return UpgradePlan(entries)


def execute_upgrade_plan(
    plan: UpgradePlan,
    revalidate_target,
    upgrade_target,
    *,
    fail_fast=False,
    cancel_requested=None,
) -> UpgradePlan:
    """Execute eligible plan entries sequentially and retain every outcome.

    Parameters
    ----------
    plan : UpgradePlan
        Previously generated plan.
    revalidate_target : callable
        Boundary that returns ``None`` when a target is still safe to mutate,
        or an explanatory string when it changed.
    upgrade_target : callable
        Existing single-package upgrade operation.
    fail_fast : bool, default=False
        Mark eligible entries after the first failure as not attempted.
    cancel_requested : callable, optional
        Boundary predicate checked before each new package operation.  A true
        result stops scheduling without interrupting an operation already in
        progress.

    Returns
    -------
    UpgradePlan
        The same plan with completed action results attached.
    """
    stopped = False
    for entry in plan.entries:
        if entry.outcome != "eligible":
            continue
        if cancel_requested is not None and cancel_requested():
            entry.outcome, entry.reason = "not-attempted", "cancelled"
            stopped = True
            continue
        if stopped:
            entry.outcome, entry.reason = "not-attempted", "fail-fast"
            continue
        problem = revalidate_target(entry.target)
        if problem:
            entry.outcome, entry.reason = "failed", problem
            stopped = fail_fast
            continue
        entry.result = upgrade_target(entry.target)
        if entry.result.ok:
            entry.outcome = "upgraded" if entry.result.changed or entry.result.status in {"installed-update", "downloaded"} else "current"
        else:
            entry.outcome, entry.reason = "failed", "upgrade-failed"
            stopped = fail_fast
    return plan


def _targets_in_upgrade_order(inventory: ManagerInventory, scopes: set[Scope]) -> list[ManagedTarget]:
    """Return selected targets in the manager's user-then-system order."""
    selected = [target for target in inventory.targets if target.scope in scopes]
    return sorted(
        selected,
        key=lambda target: (
            _scope_sort_key(target.scope),
            target.package.selector.casefold(),
            target.package.selector,
        ),
    )


_VARIABLE_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def load_manager_config(path: Path) -> ManagerConfig:
    """Read and strictly validate one schema-version-two manager file."""
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ConfigValidationError(f"Manager configuration is not a regular file: {path}")
    try:
        raw = read_toml_file(path)
    except Exception as exc:
        raise ConfigValidationError(f"Could not read manager configuration {path}: {exc}") from exc
    if "mode" not in raw or "schema_version" not in raw or "packages" not in raw:
        raise ConfigValidationError(
            "Invalid manager configuration: missing required top-level key(s)"
        )
    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != 2:
        raise ConfigValidationError(
            "schema_version must be the integer 2; older manager configurations "
            "must be explicitly migrated"
        )
    required_top_level = {"mode", "schema_version", "packages", "bin", "registry"}
    allowed_top_level = required_top_level | {"shims"}
    if not required_top_level.issubset(raw) or not set(raw).issubset(allowed_top_level):
        unknown = sorted(set(raw) - allowed_top_level)
        missing = sorted(required_top_level - set(raw))
        parts = []
        if unknown:
            parts.append(f"unknown top-level key(s): {', '.join(unknown)}")
        if missing:
            parts.append(f"missing top-level key(s): {', '.join(missing)}")
        raise ConfigValidationError(
            "Invalid manager configuration: " + "; ".join(parts)
        )
    if raw["mode"] != "manager":
        raise ConfigValidationError("mode must be exactly 'manager'")
    packages = raw["packages"]
    if not isinstance(packages, dict) or set(packages) != {"system", "user"}:
        if not isinstance(packages, dict):
            raise ConfigValidationError("[packages] must be a table containing system and user")
        unknown = sorted(set(packages) - {"system", "user"})
        missing = sorted({"system", "user"} - set(packages))
        details = []
        if unknown:
            details.append(f"unknown key(s): {', '.join(unknown)}")
        if missing:
            details.append(f"missing key(s): {', '.join(missing)}")
        raise ConfigValidationError("Invalid [packages] table: " + "; ".join(details))

    roots = {
        name: _expand_manager_path(value, path.parent, name)
        for name, value in packages.items()
    }
    system_root, user_root = roots["system"], roots["user"]
    if _same_or_nested(system_root, user_root):
        raise ConfigValidationError("Configured system and user roots must be distinct and non-nested")
    shims_table = raw.get("shims", {"linkage": "dynamic"})
    if not isinstance(shims_table, dict) or set(shims_table) != {"linkage"}:
        raise ConfigValidationError("Invalid [shims] table: expected exactly linkage")
    if shims_table["linkage"] not in {"dynamic", "static"}:
        raise ConfigValidationError("[shims].linkage must be exactly 'dynamic' or 'static'")
    bin_table = raw.get("bin")
    registry_table = raw.get("registry")
    if not isinstance(bin_table, dict) or set(bin_table) != {"system", "user"}:
        raise ConfigValidationError("Invalid [bin] table: expected exactly system and user")
    if not isinstance(registry_table, dict) or set(registry_table) != {"cache", "channel"}:
        raise ConfigValidationError("Invalid [registry] table: expected exactly cache and channel")
    bins = {
        name: _expand_manager_path(value, path.parent, name)
        for name, value in bin_table.items()
    }
    if bins["system"] == bins["user"]:
        raise ConfigValidationError("Configured system and user bin directories must be distinct")
    cache = _expand_manager_path(registry_table["cache"], path.parent, "cache")
    if registry_table["channel"] != "stable":
        raise ConfigValidationError("[registry].channel must be exactly 'stable'")
    if _same_or_nested(cache, system_root) or _same_or_nested(cache, user_root):
        raise ConfigValidationError("Registry cache must be outside both package roots")
    for field_name, value in (
        ("bin.system", bins["system"]),
        ("bin.user", bins["user"]),
        ("registry.cache", cache),
    ):
        if value.exists() and not value.is_dir():
            raise ConfigValidationError(f"Configured {field_name} path is not a directory: {value}")
    return ManagerConfig(
        path,
        system_root,
        user_root,
        system_bin=bins["system"],
        user_bin=bins["user"],
        registry_cache=cache,
        channel="stable",
        shim_linkage=shims_table["linkage"],
        schema_version=2,
    )


def discover_manager_config(
    explicit: Path | None = None,
    *,
    module_directory: Path | None = None,
    gupkg_home: str | None = None,
    appdata: str | None = None,
) -> Path | None:
    """Select a manager file from the fixed, non-working-directory locations."""
    if explicit is not None:
        return Path(explicit).expanduser()
    candidates = []
    if module_directory is not None:
        candidates.append(Path(module_directory) / "gupkg-config.toml")
    home = gupkg_home if gupkg_home is not None else os.environ.get("GUPKG_HOME")
    if home:
        candidates.append(Path(home) / "gupkg-config.toml")
    roaming_root = appdata if appdata is not None else os.environ.get("APPDATA")
    if roaming_root:
        candidates.append(Path(roaming_root) / "gupkg" / "gupkg-config.toml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def installation_context(
    config: ManagerConfig, scope: Scope, *, shim_linkage: str | None = None
) -> InstallationContext:
    """Build the manager-owned destinations for one selected scope."""
    if scope == Scope.AUTO:
        raise ValueError("Installation context requires an explicit scope")
    if scope == Scope.USER:
        collection_root = config.user_root
        bin_dir = config.user_bin
        if bin_dir is None:
            from .layout import compute_scope_paths

            bin_dir = compute_scope_paths(scope)["bin_dir"]
    else:
        collection_root = config.system_root
        bin_dir = config.system_bin
        if bin_dir is None:
            from .layout import compute_scope_paths

            bin_dir = compute_scope_paths(scope)["bin_dir"]
    try:
        from .layout import compute_scope_paths

        shortcut_root = compute_scope_paths(scope)["shortcut_root"]
    except (RuntimeError, ValueError, OSError):
        shortcut_root = None
    return InstallationContext(
        scope=scope,
        collection_root=collection_root,
        bin_dir=bin_dir,
        manager_config=config.path,
        shortcut_root=shortcut_root,
        registry_cache=config.registry_cache,
        shim_linkage=shim_linkage or config.shim_linkage,
    )


def manager_config_text(config: ManagerConfig) -> str:
    """Render a reviewed schema-version-two manager configuration."""
    user_bin = config.user_bin
    system_bin = config.system_bin
    if user_bin is None or system_bin is None:
        from .layout import compute_scope_paths

        user_bin = user_bin or compute_scope_paths(Scope.USER)["bin_dir"]
        system_bin = system_bin or compute_scope_paths(Scope.MACHINE)["bin_dir"]
    cache = config.registry_cache or (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "gupkg"
        / "registry"
    )
    def toml_path(value: Path) -> str:
        """Render a Windows path as a TOML literal string."""
        return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"

    return (
        'mode = "manager"\n'
        'schema_version = 2\n\n'
        '[packages]\n'
        f"system = {toml_path(config.system_root)}\n"
        f"user = {toml_path(config.user_root)}\n\n"
        '[bin]\n'
        f"system = {toml_path(system_bin)}\n"
        f"user = {toml_path(user_bin)}\n\n"
        '[registry]\n'
        f"cache = {toml_path(cache)}\n"
        'channel = "stable"\n\n'
        '[shims]\n'
        f'linkage = "{config.shim_linkage}"\n'
    )


def default_manager_config_path() -> Path:
    """Return the per-user location used to initialize manager mode.

    Returns
    -------
    Path
        The roaming per-user manager configuration path.

    Raises
    ------
    ValueError
        If ``APPDATA`` is not available to identify the per-user location.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("APPDATA is not set; cannot initialize manager mode")
    return Path(appdata) / "gupkg" / "gupkg-config.toml"


def default_manager_config(path: Path | None = None) -> ManagerConfig:
    """Build the reviewed manager configuration used by interactive initialization.

    Parameters
    ----------
    path : Path, optional
        Destination for the configuration file. When omitted, the per-user
        roaming manager location is used.

    Returns
    -------
    ManagerConfig
        Schema-version-two defaults for the user and system collections,
        executable directories, and registry cache.

    Raises
    ------
    ValueError
        If the Windows environment does not identify the required default
        locations.
    """
    config_path = Path(path) if path is not None else default_manager_config_path()
    user_profile = os.environ.get("USERPROFILE")
    local_app_data = os.environ.get("LOCALAPPDATA")
    system_drive = os.environ.get("SYSTEMDRIVE")
    if not user_profile or not local_app_data or not system_drive:
        raise ValueError(
            "USERPROFILE, LOCALAPPDATA, and SYSTEMDRIVE are required to initialize manager mode"
        )
    if len(system_drive) == 2 and system_drive[1] == ":":
        system_drive += "\\"
    system_root = Path(system_drive) / "opt"
    user_root = Path(user_profile) / "opt"
    return ManagerConfig(
        path=config_path,
        system_root=system_root,
        user_root=user_root,
        system_bin=Path(system_drive) / "bin",
        user_bin=Path(user_profile) / "bin",
        registry_cache=Path(local_app_data) / "gupkg" / "registry",
        channel="stable",
        shim_linkage="dynamic",
        schema_version=2,
    )


def migrate_manager_config(path: Path, output: Path | None = None) -> Path:
    """Write a schema-version-two copy of a validated manager file."""
    config = load_manager_config(path)
    destination = Path(output) if output is not None else Path(path)
    write_text_atomic(destination, manager_config_text(config))
    return destination


def discover_manager(config: ManagerConfig, *, max_depth: int = 8) -> ManagerInventory:
    """Discover both configured roots and return scoped, deterministic targets."""
    scopes: list[ManagedScope] = []
    targets: list[ManagedTarget] = []
    for scope, root in ((Scope.USER, config.user_root), (Scope.MACHINE, config.system_root)):
        diagnostics: list[str] = []
        inventory: Inventory | None = None
        complete = True
        try:
            if not root.exists():
                raise OSError("root does not exist")
            if not root.is_dir():
                raise OSError("root is not a directory")
            # Let the collection boundary report traversal failures verbatim.
            inventory = discover_collection(root, max_depth=max_depth)
            complete = inventory.complete
            diagnostics.extend(inventory.diagnostics)
        except OSError as exc:
            complete = False
            diagnostics.append(f"Cannot access {scope_name(scope)} root {root}: {exc}")
        managed_scope = ManagedScope(scope, root, inventory, complete, diagnostics)
        scopes.append(managed_scope)
        if inventory is not None:
            targets.extend(_managed_targets(scope, inventory))
    targets.sort(key=lambda target: (target.package.selector.casefold(), target.package.selector, _scope_sort_key(target.scope)))
    return ManagerInventory(config, scopes, targets)


def select_target(inventory: ManagerInventory, selector: str, scope: Scope | None = None) -> ManagedTarget:
    """Select a full target ID or an unambiguous manager selector."""
    matches = [target for target in inventory.targets if target.target_id.casefold() == selector.casefold()]
    if not matches:
        matches = [target for target in inventory.targets if target.package.selector.casefold() == selector.casefold()]
    if scope is not None:
        matches = [target for target in matches if target.scope == scope]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(target.target_id for target in matches)
        raise ValueError(f"Target selector is ambiguous: {selector}; choose one of: {choices}")
    raise ValueError(f"Managed target was not found: {selector}")


def _expand_manager_path(value: object, base: Path, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"[packages].{field_name} must be a nonempty string")
    env = {key.casefold(): val for key, val in os.environ.items()}
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.casefold() not in env:
            raise ConfigValidationError(f"[packages].{field_name} references unresolved variable %{name}%")
        return env[name.casefold()]
    expanded = _VARIABLE_RE.sub(replace, value.strip())
    path = Path(expanded).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _same_or_nested(left: Path, right: Path) -> bool:
    try:
        return left == right or left.is_relative_to(right) or right.is_relative_to(left)
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _managed_targets(scope: Scope, inventory: Inventory) -> list[ManagedTarget]:
    result = []
    for package in inventory.packages:
        current = inspect_current(package.root)
        diagnostics = list(package.diagnostics) + list(current.diagnostics)
        installed_version = current.version_path.name if current.version_path else None
        local_version = max((manifest.version_path.name for manifest in package.manifests), key=cmp_to_key(compare_package_versions), default=None)
        # Validate each discovered manifest through the existing package
        # configuration boundary so inventory health reflects real package
        # semantics rather than merely the presence of a file.
        for manifest in package.manifests:
            identity = PackageIdentity.from_version_path(
                package.root, manifest.version_path, is_current=manifest.version_path == current.version_path
            )
            try:
                read_runtime_config(identity)
            except Exception as exc:
                diagnostics.append(f"Invalid manifest {manifest.path}: {exc}")
        health = "healthy" if not diagnostics else "unhealthy"
        if not inventory.complete:
            health = "incomplete"
        result.append(ManagedTarget(
            f"{scope_id(scope)}:{package.selector}", scope, package,
            current.status, installed_version, local_version, health,
            diagnostics=diagnostics,
        ))
    return result


def scope_id(scope: Scope) -> str:
    """Return the stable lowercase manager identifier for a scope."""
    return "user" if scope == Scope.USER else "system"


def scope_name(scope: Scope) -> str:
    """Return the user-facing manager scope name."""
    return "User" if scope == Scope.USER else "System"


def _scope_sort_key(scope: Scope) -> int:
    return 0 if scope == Scope.USER else 1
