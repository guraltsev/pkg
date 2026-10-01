"""Inspect and repair the standalone ``gupkg`` package integration.

The distribution domain identifies the running version, validates the local
runtime and reports scope shims without owning generic manifest parsing or
component installation. Repair delegates to the existing package workflow so
standalone and ordinary package installs share activation and shim behavior.

Usage and API
-------------
Call ``standalone_version_root(...)`` to identify a packaged runtime,
``self_status(...)`` for diagnostics, and ``repair_self(...)`` to request a
normal package repair for one scope.

Implementation Approach
-----------------------
Package identity is derived from the executable module location and every
shim is inspected as a separate scope integration. Runtime checks are
read-only; repair validates the selected version before delegating mutation to
the existing installer.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import ActionResult, ConfigValidationError, EXIT_MUTATION_ERROR, EXIT_SUCCESS, Scope


@dataclass(frozen=True)
class SelfShim:
    """Describe one detectable scope shim and its configured runtime target."""

    scope: Scope
    path: Path
    config_path: Path
    target: str | None
    healthy: bool
    diagnostic: str | None = None


def standalone_version_root(module_file: Path | None = None) -> Path:
    """Return the standalone version directory containing the running module."""
    module_path = Path(module_file or __file__).resolve()
    candidate = module_path.parent.parent
    if not (candidate / "pkg.toml").is_file():
        raise ConfigValidationError(
            "The running gupkg is not a standalone packaged version; use a release bootstrap"
        )
    return candidate


def self_status(
    *,
    version_root: Path | None = None,
    user_bin: Path | None = None,
    system_bin: Path | None = None,
) -> dict[str, Any]:
    """Return read-only standalone version, runtime, and shim diagnostics."""
    root = Path(version_root) if version_root is not None else standalone_version_root()
    runtime = root / "gupkg" / "python" / "python.exe"
    shims = [
        _inspect_shim(Scope.USER, user_bin or _user_bin(), root),
        _inspect_shim(Scope.MACHINE, system_bin or _system_bin(), root),
    ]
    return {
        "version_root": root,
        "runtime": runtime,
        "runtime_healthy": _runtime_healthy(runtime, root),
        "shims": shims,
    }


def repair_self(
    *,
    scope: Scope,
    version_root: Path | None = None,
    install_context=None,
) -> ActionResult:
    """Repair one standalone scope integration through ``install_package``."""
    try:
        root = Path(version_root) if version_root is not None else standalone_version_root()
        runtime = root / "gupkg" / "python" / "python.exe"
        if not _runtime_healthy(runtime, root):
            return ActionResult(
                False,
                errors=[
                    f"Embedded runtime is missing or unhealthy: {runtime}. Run the release bootstrap or version-local gupkg.cmd."
                ],
                exit_code=EXIT_MUTATION_ERROR,
            )
        from .gupkg import install_package

        result = install_package(root, scope=scope, install_context=install_context)
        return result
    except (ConfigValidationError, OSError, ValueError) as exc:
        return ActionResult(False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)


def _runtime_healthy(runtime: Path, version_root: Path) -> bool:
    """Check the minimum packaged runtime contract without importing it."""
    return (
        runtime.is_file()
        and (version_root / "gupkg" / "__main__.py").is_file()
        and (version_root / "pkg.toml").is_file()
    )


def _inspect_shim(scope: Scope, bin_dir: Path, version_root: Path) -> SelfShim:
    """Inspect one scope shim and its adjacent native-launcher config."""
    shim_path = Path(bin_dir) / "gupkg.exe"
    config_path = shim_path.with_name("gupkg.config.toml")
    if not shim_path.is_file():
        return SelfShim(scope, shim_path, config_path, None, False, "shim is missing")
    if not config_path.is_file():
        return SelfShim(scope, shim_path, config_path, None, False, "shim config is missing")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return SelfShim(scope, shim_path, config_path, None, False, f"invalid shim config: {exc}")

    target = config.get("target")
    if not isinstance(target, str) or not target:
        target = None
    if not target:
        return SelfShim(scope, shim_path, config_path, None, False, "shim target is missing")

    # Standalone shims delegate to the sibling batch bootstrap through cmd.exe;
    # that bootstrap owns interpreter selection and may use system Python.
    expanded_target = os.path.expandvars(target)
    target_path = Path(expanded_target)
    if not target_path.is_absolute():
        target_path = config_path.parent / target_path
    target_path = target_path.resolve()
    healthy = target_path.is_file() and target_path.is_relative_to(version_root.resolve())
    if target_path.name.casefold() == "cmd.exe":
        working_dir = config.get("working_dir") or "."
        working_path = Path(os.path.expandvars(str(working_dir)))
        if not working_path.is_absolute():
            working_path = config_path.parent / working_path
        command_path = None
        for argument in config.get("argument", []):
            value = argument.get("value") if isinstance(argument, dict) else None
            if isinstance(value, str) and value.casefold().endswith(".cmd"):
                command_path = Path(os.path.expandvars(value))
        if command_path is not None:
            if not command_path.is_absolute():
                command_path = working_path / command_path
            command_path = command_path.resolve()
            healthy = command_path.is_file() and command_path.is_relative_to(version_root.resolve())
    return SelfShim(
        scope,
        shim_path,
        config_path,
        target,
        healthy,
        None if healthy else "shim target is missing or escapes the package version",
    )


def _user_bin() -> Path:
    """Return the conventional user command directory."""
    return Path(os.environ.get("USERPROFILE", Path.home())) / "bin"


def _system_bin() -> Path:
    """Return the conventional machine command directory."""
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    return Path(drive) / "bin"
