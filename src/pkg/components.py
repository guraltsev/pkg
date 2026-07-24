"""Install package shortcuts, environment settings, PATH entries, and wrappers.

Normalized configuration rows are expanded against the active package view and
applied to the requested user or machine scope. Each component reports changes,
warnings, and failures without hiding partial mutations from the caller.

Implementation Approach
-----------------------
Every component validates expansion results before crossing its filesystem or
registry boundary. The coordinator applies the fixed component sequence and
combines individual outcomes into one install-step result.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from .core import (
    ExpansionMode,
    PackageIdentity,
    Scope,
    StepResult,
    expand_text,
    log_error,
    log_info,
    log_warning,
    write_bytes_atomic,
)
from .windows import (
    broadcast_environment_change,
    create_shortcut,
    environment_registry_location,
    read_registry_value,
    require_winreg,
    write_registry_value,
)
from .layout import _warn_if_output_path_is_unusual


def install_shortcuts(
    shortcuts: List[Dict[str, str]],
    identity: PackageIdentity,
    scope_paths: Dict[str, Path],
) -> StepResult:
    """Install every shortcut declared by a package.

    Parameters
    ----------
    shortcuts : List[Dict[str, str]]
        Normalized ``[[shortcut]]`` rows from the runtime config.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope_paths : Dict[str, Path]
        Scope-specific filesystem locations computed for install.
    Returns
    -------
    StepResult
        A :class:`StepResult` summarizing the shortcut step.

    """
    result = StepResult(ok=True, changed=False)
    shortcut_root = scope_paths["shortcut_root"]
    for shortcut_entry in shortcuts:
        raw_name = shortcut_entry.get("name", "")
        raw_display_name = raw_name or "<unnamed>"

        try:
            name_expansion = expand_text(raw_name, identity, ExpansionMode.GENERAL)
            if name_expansion.unresolved:
                unresolved = ", ".join(name_expansion.unresolved)
                raise ValueError(
                    f"shortcut name for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_name = name_expansion.value.strip()

            target_expansion = expand_text(
                shortcut_entry.get("targetPath", ""), identity, ExpansionMode.GENERAL
            )
            if target_expansion.unresolved:
                unresolved = ", ".join(target_expansion.unresolved)
                raise ValueError(
                    f"shortcut targetPath for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_target = target_expansion.value.strip()

            arguments_expansion = expand_text(
                shortcut_entry.get("arguments", ""), identity, ExpansionMode.GENERAL
            )
            if arguments_expansion.unresolved:
                unresolved = ", ".join(arguments_expansion.unresolved)
                raise ValueError(
                    f"shortcut arguments for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_arguments = arguments_expansion.value

            working_directory_expansion = expand_text(
                shortcut_entry.get("workingDirectory", ""),
                identity,
                ExpansionMode.GENERAL,
            )
            if working_directory_expansion.unresolved:
                unresolved = ", ".join(working_directory_expansion.unresolved)
                raise ValueError(
                    f"shortcut workingDirectory for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_working_directory = working_directory_expansion.value

            icon_location_expansion = expand_text(
                shortcut_entry.get("iconLocation", ""), identity, ExpansionMode.GENERAL
            )
            if icon_location_expansion.unresolved:
                unresolved = ", ".join(icon_location_expansion.unresolved)
                raise ValueError(
                    f"shortcut iconLocation for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_icon_location = icon_location_expansion.value

            description_expansion = expand_text(
                shortcut_entry.get("description", ""), identity, ExpansionMode.GENERAL
            )
            if description_expansion.unresolved:
                unresolved = ", ".join(description_expansion.unresolved)
                raise ValueError(
                    f"shortcut description for '{raw_display_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_description = description_expansion.value

            missing: List[str] = []
            if not expanded_name:
                missing.append("name")
            if not expanded_target:
                missing.append("targetPath")
            if missing:
                raise ValueError(
                    f"shortcut '{raw_display_name}' is missing required field(s) after expansion: {', '.join(missing)}"
                )

            shortcut_root.mkdir(parents=True, exist_ok=True)
            shortcut_path = shortcut_root / expanded_name
            if shortcut_path.suffix.lower() != ".lnk":
                shortcut_path = shortcut_path.with_suffix(".lnk")
            _warn_if_output_path_is_unusual(
                "shortcut", shortcut_root, expanded_name, shortcut_path
            )
            shortcut_path.parent.mkdir(parents=True, exist_ok=True)

            create_shortcut(
                shortcut_path,
                expanded_target,
                arguments=expanded_arguments,
                working_directory=expanded_working_directory,
                icon_location=expanded_icon_location,
                description=expanded_description,
            )
            log_info(f"SHORTCUT: created: {shortcut_path.name}")
            result.changed = True
            continue

        except Exception as exc:
            name = raw_name or "unknown"
            log_error(f"SHORTCUT error creating {name}: {exc}")
            message = f"Failed to create shortcut '{name}': {exc}"

        result.ok = False
        log_error(message)
        result.errors.append(message)

    return result


def set_environment_variable(
    name: str, value: str, scope: Scope, expand: bool = True
) -> bool:
    """Set one environment variable in the Windows registry.

    Parameters
    ----------
    name : str
        Variable name.
    value : str
        Variable value.
    scope : Scope
        Target installation scope.
    expand : bool
        Whether to store the value as ``REG_EXPAND_SZ``.

    Returns
    -------
    bool
        ``True`` on success; ``False`` on failure.

    """
    try:
        root, subkey = environment_registry_location(scope)
        reg = require_winreg()
        reg_type = reg.REG_EXPAND_SZ if expand else reg.REG_SZ
        write_registry_value(root, subkey, name, value, reg_type)
        log_info(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
        try:
            broadcast_environment_change()
        except Exception as exc:
            log_warning(f"failed to broadcast environment change notification: {exc}")
        return True
    except PermissionError:
        log_error(
            f"Insufficient permissions to set {scope.value} environment variable: {name}"
        )
        return False
    except Exception as exc:
        log_error(f"ENVIRONMENT error setting {name}: {exc}")
        return False


def install_environment_variables(
    environment_entries: List[Dict[str, str]],
    identity: PackageIdentity,
    scope: Scope,
) -> StepResult:
    """Install every environment variable declared by a package.

    Parameters
    ----------
    environment_entries : List[Dict[str, str]]
        Normalized ``[[environment]]`` rows.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope : Scope
        Target install scope for registry writes.

    Returns
    -------
    StepResult
        A :class:`StepResult` summarizing the environment-variable step.

    """
    result = StepResult(ok=True, changed=False)
    for env_var in environment_entries:
        name = env_var.get("Name", "").strip()
        value = env_var.get("Value", "")
        if not name:
            message = f"Environment variable entry is missing Name: {env_var}"
            log_error(message)
            result.ok = False
            result.errors.append(message)
            continue
        expansion = expand_text(str(value), identity, ExpansionMode.GENERAL)
        if expansion.unresolved:
            unresolved = ", ".join(expansion.unresolved)
            message = f"Environment variable '{name}' contains unresolved variable(s): {unresolved}"
            log_error(message)
            result.ok = False
            result.errors.append(message)
            continue
        ok = set_environment_variable(name, expansion.value, scope, expand=True)
        if ok:
            result.changed = True
            continue
        message = f"Failed to set environment variable: {name}"
        log_error(message)
        result.ok = False
        result.errors.append(message)
    return result


def _path_key(path_value: str) -> str:
    """Normalize a PATH entry for de-duplication.

    Parameters
    ----------
    path_value : str
        Original PATH entry.

    Returns
    -------
    str
        A normalized, case-insensitive comparison key.

    """
    key = os.path.normcase(os.path.normpath(path_value))
    return key.rstrip("\\/")


def get_current_path(scope: Scope) -> List[str]:
    """Read PATH entries from the Windows registry.

    Parameters
    ----------
    scope : Scope
        Installation scope whose PATH should be read.

    Returns
    -------
    List[str]
        A list of PATH components. Missing values return an empty list.

    """
    try:
        root, subkey = environment_registry_location(scope)
        value, reg_type = read_registry_value(root, subkey, "Path")
        reg = require_winreg()
        if reg_type in (reg.REG_EXPAND_SZ, reg.REG_SZ):
            return [item.strip() for item in str(value).split(";") if item.strip()]
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_error(f"PATH error reading {scope.value} PATH: {exc}")

    return []


def set_path(path_entries: List[str], scope: Scope) -> bool:
    """Write PATH entries to the registry.

    Parameters
    ----------
    path_entries : List[str]
        Ordered PATH components to store.
    scope : Scope
        Installation scope whose PATH should be updated.

    Returns
    -------
    bool
        ``True`` on success; otherwise ``False``.

    """
    try:
        path_value = ";".join(path_entries)
        root, subkey = environment_registry_location(scope)
        reg = require_winreg()
        write_registry_value(root, subkey, "Path", path_value, reg.REG_EXPAND_SZ)
        try:
            broadcast_environment_change()
        except Exception as exc:
            log_warning(f"failed to broadcast environment change notification: {exc}")
        return True
    except PermissionError:
        log_error(f"Insufficient permissions to set {scope.value} PATH")
        return False
    except Exception as exc:
        log_error(f"PATH error setting {scope.value} PATH: {exc}")
        return False


def add_to_path(
    new_entries: List[str], identity: PackageIdentity, scope: Scope
) -> StepResult:
    """Append directories to PATH while avoiding duplicates.

    Parameters
    ----------
    new_entries : List[str]
        PATH entries that may still contain ``$App``-style
            variables.
    identity : PackageIdentity
        Package identity used for expansion.
    scope : Scope
        Installation scope whose PATH should be updated.

    Returns
    -------
    StepResult
        A :class:`StepResult` summarizing the PATH update.

    """
    result = StepResult(ok=True, changed=False)
    valid_entries: List[str] = []

    for entry in new_entries:
        expansion = expand_text(str(entry), identity, ExpansionMode.GENERAL)
        if expansion.unresolved:
            unresolved = ", ".join(expansion.unresolved)
            message = (
                f"PATH entry '{entry}' contains unresolved variable(s): {unresolved}"
            )
            log_error(message)
            result.ok = False
            result.errors.append(message)
            continue

        expanded = expansion.value.strip()
        if expanded == "":
            message = (
                f"PATH entry '{entry}' expands to an empty value and will not be added."
            )
            log_error(message)
            result.ok = False
            result.errors.append(message)
            continue

        normalized = os.path.normpath(expanded)
        if normalized == "":
            message = f"PATH entry '{entry}' normalized to an empty value and will not be added."
            log_error(message)
            result.ok = False
            result.errors.append(message)
            continue

        valid_entries.append(normalized)

    if not valid_entries:
        return result if result.errors else StepResult(ok=True, changed=False)

    current_path = get_current_path(scope)
    updated_path = current_path.copy()
    existing_keys = {_path_key(item) for item in current_path if item}
    added_entries: List[str] = []

    for entry in valid_entries:
        key = _path_key(entry)
        if key not in existing_keys:
            updated_path.append(entry)
            existing_keys.add(key)
            added_entries.append(entry)
            log_info(f"PATH: adding to {scope.value} scope: {entry}")

    if not added_entries:
        return result

    if set_path(updated_path, scope):
        result.changed = True
        return result

    message = f"Failed to update {scope.value} PATH."
    log_error(message)
    result.ok = False
    result.errors.append(message)
    return result


def ensure_bin_in_path(
    scope_paths: Dict[str, Path], identity: PackageIdentity, scope: Scope
) -> StepResult:
    """Ensure the per-scope ``bin`` directory exists and is on PATH.

    Parameters
    ----------
    scope_paths : Dict[str, Path]
        Scope-specific filesystem locations computed for install.
    identity : PackageIdentity
        Package identity passed through to PATH expansion helpers.
    scope : Scope
        Installation scope whose PATH should include ``bin``.

    Returns
    -------
    StepResult
        A :class:`StepResult` summarizing the bin-directory and PATH work.

    """
    bin_dir = scope_paths["bin_dir"]

    changed = False
    try:
        existed_before = bin_dir.exists()
        bin_dir.mkdir(parents=True, exist_ok=True)
        changed = not existed_before
    except OSError as exc:
        return StepResult(
            ok=False, errors=[f"Failed to create bin directory {bin_dir}: {exc}"]
        )

    current_path = get_current_path(scope)
    bin_dir_str = str(bin_dir)
    bin_key = _path_key(bin_dir_str)
    current_keys = {_path_key(item) for item in current_path if item}
    if bin_key not in current_keys:
        path_result = add_to_path([bin_dir_str], identity, scope)
        path_result.changed = path_result.changed or changed
        return path_result

    return StepResult(ok=True, changed=changed)


def install_wrappers(
    wrapper_entries: List[Dict[str, str]],
    identity: PackageIdentity,
    scope_paths: Dict[str, Path],
) -> StepResult:
    """Install every wrapper declared by a package.

    Parameters
    ----------
    wrapper_entries : List[Dict[str, str]]
        Normalized ``[[bin]]`` rows from the runtime config.
    identity : PackageIdentity
        Package identity used for variable expansion.
    scope_paths : Dict[str, Path]
        Scope-specific filesystem locations computed for install.

    Returns
    -------
    StepResult
        A :class:`StepResult` summarizing the wrapper-install step.

    """
    result = StepResult(ok=True, changed=False)
    bin_dir = scope_paths["bin_dir"]
    for wrapper_entry in wrapper_entries:
        raw_name = wrapper_entry.get("name", "")
        try:
            raw_content = wrapper_entry.get("content", "")
            if not raw_name:
                raise ValueError("wrapper entry is missing name")

            name_expansion = expand_text(raw_name, identity, ExpansionMode.GENERAL)
            if name_expansion.unresolved:
                unresolved = ", ".join(name_expansion.unresolved)
                raise ValueError(
                    f"wrapper name for '{raw_name}' contains unresolved variable(s): {unresolved}"
                )
            expanded_name = name_expansion.value.strip()

            content_expansion = expand_text(raw_content, identity, ExpansionMode.SCRIPT)
            if content_expansion.unresolved:
                unresolved = ", ".join(content_expansion.unresolved)
                raise ValueError(
                    f"wrapper '{expanded_name or raw_name}' content contains unresolved variable(s): {unresolved}"
                )
            expanded_content = content_expansion.value

            bin_dir.mkdir(parents=True, exist_ok=True)

            wrapper_path = bin_dir / expanded_name
            _warn_if_output_path_is_unusual("bin", bin_dir, expanded_name, wrapper_path)
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)

            extension = wrapper_path.suffix.lower()
            if extension in (".cmd", ".bat"):
                try:
                    desired_bytes = expanded_content.encode("ascii")
                except UnicodeEncodeError:
                    log_warning(
                        f"non-ASCII content in {extension} wrapper; writing UTF-8 with BOM: {wrapper_path.name}"
                    )
                    desired_bytes = expanded_content.encode("utf-8-sig")
            else:
                desired_bytes = expanded_content.encode("utf-8")

            existed_before = wrapper_path.exists()
            if existed_before:
                try:
                    if wrapper_path.read_bytes() == desired_bytes:
                        log_info(f"BIN: up-to-date: {wrapper_path}")
                        continue
                except OSError:
                    pass

            write_bytes_atomic(wrapper_path, desired_bytes)
            action = "updated" if existed_before else "created"
            log_info(f"BIN: {action}: {wrapper_path}")
            result.changed = True
            continue

        except Exception as exc:
            name = raw_name or "unknown"
            log_error(f"BIN error creating {name}: {exc}")
            message = f"Failed to create wrapper '{name}': {exc}"

        log_error(message)
        result.ok = False
        result.errors.append(message)
    return result


def install_components(
    identity: PackageIdentity,
    scope: Scope,
    scope_paths: Dict[str, Path],
    runtime_config: Dict[str, Any],
) -> StepResult:
    """Run the fixed install sequence for one package version.

    The order here is deliberate and intentionally explicit. ``pkg`` does not
    have a pluggable install pipeline, so keeping the sequence inline makes the
    state transitions easier to audit:

    1. create shortcuts
    2. write environment variables
    3. when wrappers are declared, ensure the scope ``bin`` directory exists
       and is on ``PATH``
    4. add package-specific extra ``PATH`` entries
    5. create wrapper/bin files

    Parameters
    ----------
    identity : PackageIdentity
        Package version being installed.
    scope : Scope
        Selected installation scope.
    scope_paths : dict[str, Path]
        Scope-specific filesystem locations computed for installation.
    runtime_config : dict[str, Any]
        Canonical normalized runtime config derived from ``pkg.toml``.
    Returns
    -------
    StepResult
        Aggregated step result for the fixed install sequence.

    """
    # Create package-owned shortcuts before mutating PATH or wrapper files so a
    # partial install still exposes the most user-visible entrypoints first.
    shortcut_result = StepResult(ok=True, changed=False)
    if runtime_config["shortcut"]:
        log_info("")
        log_info("Creating shortcuts...")
        shortcut_result = install_shortcuts(
            runtime_config["shortcut"],
            identity,
            scope_paths,
        )

    # Apply environment variables next so later wrapper and PATH work can rely
    # on the persisted scope values that users expect after installation.
    environment_result = StepResult(ok=True, changed=False)
    if runtime_config["environment"]:
        log_info("")
        log_info("Setting environment variables...")
        environment_result = install_environment_variables(
            runtime_config["environment"], identity, scope
        )

    # Treat PATH management as one phase because wrapper creation may require a
    # shared ``bin`` directory as well as package-specific extra entries.
    bin_path_result = StepResult(ok=True, changed=False)
    extra_path_result = StepResult(ok=True, changed=False)
    if runtime_config["bin"] or runtime_config["path"]:
        log_info("")
        log_info("Managing PATH...")

    if runtime_config["bin"]:
        bin_path_result = ensure_bin_in_path(scope_paths, identity, scope)

    if runtime_config["path"]:
        extra_path_result = add_to_path(runtime_config["path"], identity, scope)

    # Emit wrapper files last so they can target directories and PATH entries
    # that were prepared earlier in the install sequence.
    wrapper_result = StepResult(ok=True, changed=False)
    if runtime_config["bin"]:
        log_info("")
        log_info("Creating executable wrappers...")
        wrapper_result = install_wrappers(runtime_config["bin"], identity, scope_paths)

    # Merge per-step status into one result object that accurately reports
    # whether the overall install mutated state and whether any step failed.
    combined = StepResult(ok=True, changed=False)
    for step_result in (
        shortcut_result,
        environment_result,
        bin_path_result,
        extra_path_result,
        wrapper_result,
    ):
        combined.ok = combined.ok and step_result.ok
        combined.changed = combined.changed or step_result.changed
        combined.warnings.extend(step_result.warnings)
        combined.errors.extend(step_result.errors)
    if combined.errors:
        combined.ok = False
    return combined
