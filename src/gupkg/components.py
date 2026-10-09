"""Install package shortcuts, environment settings, PATH entries, and wrappers.

Normalized configuration rows are expanded against the active package view and
applied to the requested user or machine scope. Each component reports changes
and failures without hiding partial mutations from the caller; one failing row
never prevents the remaining rows from being applied.

Usage and API
-------------
``install_components(...)`` runs the fixed install sequence for one package
version and is called by ``gupkg.gupkg.install_package(...)``.

Implementation Approach
-----------------------
Every row is expanded and validated before crossing its filesystem or registry
boundary. File outputs are written atomically and skipped when already
identical, so reinstalling is an idempotent repair. The coordinator combines
the individual outcomes into one install-step result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .core import (
    ExpansionMode,
    PackageIdentity,
    Scope,
    StepResult,
    expand_text,
    log_info,
    log_warning,
    write_bytes_atomic,
)
from .layout import warn_if_output_path_is_unusual
from .windows import (
    broadcast_environment_change,
    create_shortcut,
    environment_registry_location,
    read_registry_value,
    require_winreg,
    write_registry_value,
)


# Runtime DLLs and notices that must sit beside every dynamically linked shim.
_DYNAMIC_SHIM_COMPANIONS = (
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libwinpthread-1.dll",
    "LICENSE-exe-shim-MIT.txt",
    "LICENSE-exe-shim-UNLICENSE.txt",
    "LICENSE-GCC-3.0.txt",
    "LICENSE-GCC-RUNTIME-EXCEPTION-3.1.txt",
    "LICENSE-libwinpthread-MIT.txt",
)


def install_components(
    identity: PackageIdentity,
    scope: Scope,
    scope_paths: Dict[str, Any],
    runtime_config: Dict[str, Any],
) -> StepResult:
    """Run the fixed install sequence for one package version.

    The order is deliberate and fixed: shortcuts, environment variables, the
    scope ``bin`` directory on PATH (when wrappers are declared), extra PATH
    entries, and finally wrapper files.

    Parameters
    ----------
    identity : PackageIdentity
        Package version being installed.
    scope : Scope
        Selected installation scope (user or machine).
    scope_paths : dict[str, Any]
        ``shortcut_root``, ``bin_dir``, ``shim_linkage``, and optionally
        ``collection_root`` for the selected scope.
    runtime_config : dict[str, Any]
        Canonical normalized runtime config derived from ``pkg.toml``.

    Returns
    -------
    StepResult
        Aggregated result; ``errors`` names every row that failed.

    """
    combined = StepResult(ok=True)

    def run(title: str, step) -> None:
        """Run one install phase and fold its outcome into the combined result."""
        log_info("")
        log_info(title)
        result: StepResult = step()
        combined.ok = combined.ok and result.ok
        combined.changed = combined.changed or result.changed
        combined.warnings.extend(result.warnings)
        combined.errors.extend(result.errors)

    # Shortcuts come first so a partial install still exposes the most
    # user-visible entrypoints; environment values follow so later wrappers
    # and PATH entries can rely on them.
    if runtime_config["shortcut"]:
        run("Creating shortcuts...", lambda: install_shortcuts(
            runtime_config["shortcut"], identity, scope_paths
        ))
    if runtime_config["environment"]:
        run("Setting environment variables...", lambda: install_environment_variables(
            runtime_config["environment"], identity, scope, scope_paths
        ))

    # Wrappers need the scope bin directory on PATH; package-specific PATH
    # entries follow it. Wrapper files are emitted last so they target
    # directories and PATH entries prepared earlier in the sequence.
    if runtime_config["bin"]:
        run("Managing PATH...", lambda: ensure_bin_in_path(scope_paths, identity, scope))
    if runtime_config["path"]:
        if not runtime_config["bin"]:
            log_info("")
            log_info("Managing PATH...")
        path_result = add_to_path(runtime_config["path"], identity, scope, scope_paths)
        combined.ok = combined.ok and path_result.ok
        combined.changed = combined.changed or path_result.changed
        combined.errors.extend(path_result.errors)
    if runtime_config["bin"]:
        run("Creating executable wrappers...", lambda: install_wrappers(
            runtime_config["bin"], identity, scope_paths
        ))
    return combined


# ---------------------------------------------------------------------------
# Shortcuts
# ---------------------------------------------------------------------------


def install_shortcuts(
    shortcuts: List[Dict[str, str]],
    identity: PackageIdentity,
    scope_paths: Dict[str, Any],
) -> StepResult:
    """Create every ``[[shortcut]]`` below the scope's Start Menu root.

    Parameters
    ----------
    shortcuts : List[Dict[str, str]]
        Normalized ``[[shortcut]]`` rows from the runtime config.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope_paths : Dict[str, Any]
        Scope-specific locations; ``shortcut_root`` receives the shortcuts.

    Returns
    -------
    StepResult
        Shortcut outcome; every shortcut is recreated, so success is a change.

    """
    result = StepResult(ok=True)
    shortcut_root: Path = scope_paths["shortcut_root"]
    for entry in shortcuts:
        label = entry.get("name") or "unknown"
        try:
            # Expand every field before touching the filesystem.
            fields = {
                key: _expand(entry.get(key, ""), identity, scope_paths, f"shortcut {key} for '{label}'")
                for key in ("name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description")
            }
            name, target = fields["name"].strip(), fields["targetPath"].strip()
            missing = [key for key, value in (("name", name), ("targetPath", target)) if not value]
            if missing:
                raise ValueError(
                    f"shortcut '{label}' is missing required field(s) after expansion: {', '.join(missing)}"
                )

            # Names may nest below the root; the .lnk suffix is implied.
            shortcut_path = shortcut_root / name
            if shortcut_path.suffix.lower() != ".lnk":
                shortcut_path = shortcut_path.with_suffix(".lnk")
            warn_if_output_path_is_unusual("shortcut", shortcut_root, name, shortcut_path)
            shortcut_path.parent.mkdir(parents=True, exist_ok=True)
            create_shortcut(
                shortcut_path,
                target,
                arguments=fields["arguments"],
                working_directory=fields["workingDirectory"],
                icon_location=fields["iconLocation"],
                description=fields["description"],
            )
            log_info(f"SHORTCUT: created: {shortcut_path.name}")
            result.changed = True
        except Exception as exc:
            result.ok = False
            result.errors.append(f"Failed to create shortcut '{label}': {exc}")
    return result


# ---------------------------------------------------------------------------
# Environment and PATH
# ---------------------------------------------------------------------------


def install_environment_variables(
    environment_entries: List[Dict[str, str]],
    identity: PackageIdentity,
    scope: Scope,
    install_context: Dict[str, Any] | None = None,
) -> StepResult:
    """Write every ``[[environment]]`` row as an expandable registry string.

    Parameters
    ----------
    environment_entries : List[Dict[str, str]]
        Normalized ``[[environment]]`` rows.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope : Scope
        Target install scope for registry writes.
    install_context : dict, optional
        Scope paths that supply ``$ScopeRoot`` and ``$Bin``.

    Returns
    -------
    StepResult
        Environment outcome; every written value is a change.

    """
    result = StepResult(ok=True)
    for entry in environment_entries:
        name = entry.get("Name", "").strip()
        try:
            if not name:
                raise ValueError("entry is missing Name")
            value = _expand(entry.get("Value", ""), identity, install_context, f"environment variable '{name}'")
            root, subkey = environment_registry_location(scope)
            write_registry_value(root, subkey, name, value, require_winreg().REG_EXPAND_SZ)
            log_info(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
            result.changed = True
        except PermissionError:
            result.ok = False
            result.errors.append(f"Insufficient permissions to set {scope.value} environment variable: {name}")
        except Exception as exc:
            result.ok = False
            result.errors.append(f"Failed to set environment variable '{name or entry}': {exc}")
    if result.changed:
        _broadcast_environment_change()
    return result


def ensure_bin_in_path(
    scope_paths: Dict[str, Any], identity: PackageIdentity, scope: Scope
) -> StepResult:
    """Create the scope's wrapper directory and make sure it is on PATH.

    Parameters
    ----------
    scope_paths : Dict[str, Any]
        Scope-specific locations; ``bin_dir`` is the wrapper directory.
    identity : PackageIdentity
        Package identity passed through to PATH expansion.
    scope : Scope
        Installation scope whose PATH should include ``bin_dir``.

    Returns
    -------
    StepResult
        ``changed`` is true when the directory was created or PATH rewritten.
    """
    bin_dir = Path(scope_paths["bin_dir"])
    try:
        created = not bin_dir.exists()
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return StepResult(ok=False, errors=[f"Failed to create bin directory {bin_dir}: {exc}"])
    result = add_to_path([str(bin_dir)], identity, scope, scope_paths)
    result.changed = result.changed or created
    return result


def add_to_path(
    new_entries: List[str],
    identity: PackageIdentity,
    scope: Scope,
    install_context: Dict[str, Any] | None = None,
) -> StepResult:
    """Append directories to the scope PATH, skipping entries already present.

    Entries are compared case-insensitively and without trailing separators.

    Parameters
    ----------
    new_entries : List[str]
        PATH entries that may still contain ``$App``-style variables.
    identity : PackageIdentity
        Package identity used for expansion.
    scope : Scope
        Installation scope whose PATH should be updated.
    install_context : dict, optional
        Scope paths that supply ``$ScopeRoot`` and ``$Bin``.

    Returns
    -------
    StepResult
        PATH outcome; ``changed`` is true when PATH was rewritten.

    """
    result = StepResult(ok=True)

    # Expand and normalize every entry first; invalid entries are reported
    # individually and never written.
    valid_entries: List[str] = []
    for entry in new_entries:
        try:
            expanded = _expand(str(entry), identity, install_context, f"PATH entry '{entry}'").strip()
            if not expanded:
                raise ValueError(f"PATH entry '{entry}' expands to an empty value and will not be added.")
            valid_entries.append(os.path.normpath(expanded))
        except ValueError as exc:
            result.ok = False
            result.errors.append(str(exc))

    # Read, extend, and rewrite PATH in one guarded step: a PATH that cannot
    # be read must never be replaced by only the new entries.
    try:
        current_path = get_current_path(scope)
        existing_keys = {_path_key(item) for item in current_path}
        added: List[str] = []
        for entry in valid_entries:
            if _path_key(entry) not in existing_keys:
                existing_keys.add(_path_key(entry))
                added.append(entry)
                log_info(f"PATH: adding to {scope.value} scope: {entry}")
        if not added:
            return result
        root, subkey = environment_registry_location(scope)
        write_registry_value(
            root, subkey, "Path", ";".join(current_path + added), require_winreg().REG_EXPAND_SZ
        )
        _broadcast_environment_change()
        result.changed = True
    except PermissionError:
        result.ok = False
        result.errors.append(f"Insufficient permissions to set {scope.value} PATH")
    except Exception as exc:
        result.ok = False
        result.errors.append(f"Failed to update {scope.value} PATH: {exc}")
    return result


def get_current_path(scope: Scope) -> List[str]:
    """Read the scope's registry PATH entries; a missing value is an empty PATH.

    Parameters
    ----------
    scope : Scope
        Installation scope whose PATH should be read.

    Returns
    -------
    List[str]
        Non-empty PATH components in registry order.

    """
    try:
        root, subkey = environment_registry_location(scope)
        value, reg_type = read_registry_value(root, subkey, "Path")
    except FileNotFoundError:
        return []
    reg = require_winreg()
    if reg_type not in (reg.REG_EXPAND_SZ, reg.REG_SZ):
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


def _path_key(path_value: str) -> str:
    """Normalize a PATH entry for case-insensitive de-duplication."""
    return os.path.normcase(os.path.normpath(path_value)).rstrip("\\/")


def _broadcast_environment_change() -> None:
    """Notify running applications of environment changes, warning on failure."""
    try:
        broadcast_environment_change()
    except Exception as exc:
        log_warning(f"failed to broadcast environment change notification: {exc}")


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------


def install_wrappers(
    wrapper_entries: List[Dict[str, Any]],
    identity: PackageIdentity,
    scope_paths: Dict[str, Any],
) -> StepResult:
    """Install every ``[[bin]]`` row as a native shim or a raw content file.

    Parameters
    ----------
    wrapper_entries : List[Dict[str, Any]]
        Normalized ``[[bin]]`` rows from the runtime config.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope_paths : Dict[str, Any]
        Scope-specific locations; ``bin_dir`` receives the wrappers and
        ``shim_linkage`` selects the launcher build.

    Returns
    -------
    StepResult
        Wrapper outcome; files that are already identical are not changes.

    """
    result = StepResult(ok=True)
    bin_dir: Path = scope_paths["bin_dir"]
    for entry in wrapper_entries:
        label = entry.get("name") or "unknown"
        try:
            if not entry.get("name"):
                raise ValueError("wrapper entry is missing name")
            name = _expand(entry["name"], identity, scope_paths, f"wrapper name for '{label}'").strip()

            # Shims are always .exe files; content wrappers keep their name.
            wrapper_path = bin_dir / name
            if "content" not in entry:
                if wrapper_path.suffix == "":
                    wrapper_path = wrapper_path.with_suffix(".exe")
                elif wrapper_path.suffix.lower() != ".exe":
                    raise ValueError("shim name must have no extension or use .exe")
            warn_if_output_path_is_unusual("bin", bin_dir, name, wrapper_path)
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)

            if "content" in entry:
                outputs = [(wrapper_path, _content_wrapper_bytes(entry, identity, scope_paths, wrapper_path), True)]
            else:
                outputs = _shim_outputs(entry, identity, scope_paths, wrapper_path, bin_dir)

            # Rewrite only outputs that differ so reinstalling is idempotent
            # and still repairs incomplete earlier installations.
            for output_path, desired, overwrite in outputs:
                if output_path.exists():
                    if not overwrite:
                        continue
                    try:
                        if output_path.read_bytes() == desired:
                            log_info(f"BIN: up-to-date: {output_path}")
                            continue
                    except OSError:
                        pass
                    action = "updated"
                else:
                    action = "created"
                write_bytes_atomic(output_path, desired)
                log_info(f"BIN: {action}: {output_path}")
                result.changed = True
        except Exception as exc:
            result.ok = False
            result.errors.append(f"Failed to create wrapper '{label}': {exc}")
    return result


def _content_wrapper_bytes(
    entry: Dict[str, Any], identity: PackageIdentity, scope_paths: Dict[str, Any], wrapper_path: Path
) -> bytes:
    """Render a raw ``content`` wrapper, keeping shell variables literal.

    Batch files are written as ASCII when possible because ``cmd.exe`` does
    not honor UTF-8 without a BOM.
    """
    content = _expand(
        entry["content"], identity, scope_paths, f"wrapper '{wrapper_path.name}' content",
        mode=ExpansionMode.SCRIPT,
    )
    if wrapper_path.suffix.lower() not in (".cmd", ".bat"):
        return content.encode("utf-8")
    try:
        return content.encode("ascii")
    except UnicodeEncodeError:
        log_warning(
            f"non-ASCII content in {wrapper_path.suffix.lower()} wrapper; writing "
            f"UTF-8 with BOM: {wrapper_path.name}"
        )
        return content.encode("utf-8-sig")


def _shim_outputs(
    entry: Dict[str, Any],
    identity: PackageIdentity,
    scope_paths: Dict[str, Any],
    wrapper_path: Path,
    bin_dir: Path,
) -> List[Any]:
    """Return ``(path, bytes, overwrite)`` for a native shim and its companions.

    The launcher executable and its adjacent ``.config.toml`` form the shim's
    installation unit. Shared runtime companions are written only when absent
    so a package repair never overwrites a copy installed for another command.
    """
    label = f"shim '{wrapper_path.stem}'"
    target = _expand(entry.get("target", ""), identity, scope_paths, f"{label} target")
    working_dir = _expand(entry.get("working_dir") or "", identity, scope_paths, f"{label} working_dir")
    arguments = [
        _expand(argument, identity, scope_paths, f"{label} argument") for argument in entry.get("arguments", [])
    ]

    # JSON strings are valid TOML basic strings.
    config_lines = [
        f"target = {json.dumps(target, ensure_ascii=False)}",
        f"forward_arguments = {str(entry.get('forward_args', True)).lower()}",
        f"elevate = {str(entry.get('elevate', False)).lower()}",
    ]
    if working_dir:
        config_lines.append(f"working_dir = {json.dumps(working_dir, ensure_ascii=False)}")
    for argument in arguments:
        config_lines.extend(["", "[[argument]]", f"value = {json.dumps(argument, ensure_ascii=False)}"])

    linkage = scope_paths.get("shim_linkage", "dynamic")
    if linkage not in {"dynamic", "static"}:
        raise ValueError("shim linkage must be either 'dynamic' or 'static'")
    shim_directory = Path(__file__).with_name("shim")
    shim_type = entry.get("type", "console")
    launcher = shim_directory / (
        f"shim-{shim_type}.exe" if linkage == "dynamic" else f"shim-{shim_type}.static.exe"
    )
    outputs = [
        (wrapper_path, launcher.read_bytes(), True),
        (
            wrapper_path.with_name(f"{wrapper_path.stem}.config.toml"),
            ("\n".join(config_lines) + "\n").encode("utf-8"),
            True,
        ),
    ]
    if linkage == "dynamic":
        outputs.extend(
            (bin_dir / name, (shim_directory / name).read_bytes(), False)
            for name in _DYNAMIC_SHIM_COMPANIONS
            if not (bin_dir / name).exists()
        )
    return outputs


def _expand(
    text: str,
    identity: PackageIdentity,
    install_context: Dict[str, Any] | None,
    label: str,
    *,
    mode: ExpansionMode = ExpansionMode.GENERAL,
) -> str:
    """Expand one configuration value, rejecting unresolved variables."""
    expansion = expand_text(text, identity, mode, install_context=install_context)
    if expansion.unresolved:
        raise ValueError(
            f"{label} contains unresolved variable(s): {', '.join(expansion.unresolved)}"
        )
    return expansion.value
