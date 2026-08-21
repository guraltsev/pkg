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
)
from .layout import inspect_current


@dataclass(frozen=True)
class ManagerConfig:
    """Describe validated manager configuration and its two resolved roots."""

    path: Path
    system_root: Path
    user_root: Path


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
            target.package.selector.casefold(),
            target.package.selector,
            _scope_sort_key(target.scope),
        ),
    )


_VARIABLE_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def load_manager_config(path: Path) -> ManagerConfig:
    """Read and strictly validate one ``gupkg-config.toml`` file."""
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ConfigValidationError(f"Manager configuration is not a regular file: {path}")
    try:
        raw = read_toml_file(path)
    except Exception as exc:
        raise ConfigValidationError(f"Could not read manager configuration {path}: {exc}") from exc
    if set(raw) != {"mode", "schema_version", "packages"}:
        unknown = sorted(set(raw) - {"mode", "schema_version", "packages"})
        missing = sorted({"mode", "schema_version", "packages"} - set(raw))
        parts = []
        if unknown:
            parts.append(f"unknown top-level key(s): {', '.join(unknown)}")
        if missing:
            parts.append(f"missing top-level key(s): {', '.join(missing)}")
        raise ConfigValidationError("Invalid manager configuration: " + "; ".join(parts))
    if raw["mode"] != "manager":
        raise ConfigValidationError("mode must be exactly 'manager'")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ConfigValidationError("schema_version must be the integer 1")
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
    return ManagerConfig(path, system_root, user_root)


def discover_manager(config: ManagerConfig) -> ManagerInventory:
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
            inventory = discover_collection(root)
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
