#!/usr/bin/env python3
"""Install and maintain local Windows packages declared by ``pkg.toml``.

The module is the stable executable and Python facade for package actions. It
coordinates configuration, origin population, component installation, and
updates while focused implementation domains live in the ``pkg`` package.

Usage and API
-------------
Call ``main(...)`` for command-line execution. Embedders may call
``install_package(...)``, ``update_package_config(...)``,
``convert_legacy_config(...)``, ``health_check_package(...)``,
``check_package_update(...)``, ``download_package_update(...)``, or
``install_downloaded_update(...)`` directly.

Implementation Approach
-----------------------
The facade resolves each action into one top-level workflow and delegates
platform, parsing, payload, and staging mechanics to focused runtime modules.
Directory identity and result objects cross those boundaries explicitly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Direct script execution starts with ``src/pkg`` on sys.path. Add ``src`` so
# the facade resolves this package by its canonical name without changing the
# caller's working directory.
_SRC_ROOT = Path(__file__).resolve().parent.parent
_src_root_text = str(_SRC_ROOT)
if _src_root_text in sys.path:
    sys.path.remove(_src_root_text)
sys.path.insert(0, _src_root_text)

from pkg.components import install_components  # noqa: E402
from pkg.configuration import (  # noqa: E402
    check_metadata_consistency,
    read_runtime_config,
)
from pkg.core import (  # noqa: E402
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    log_error,
    log_info,
    log_warning,
    read_toml_file,
    write_text_atomic,
)
from pkg.layout import (  # noqa: E402
    compute_scope_paths,
    resolve_input_path,
    update_current_junction_if_needed,
)
from pkg.legacy_to_pkg_toml import convert_legacy_directory  # noqa: E402
from pkg.metadata import update_config_file  # noqa: E402
from pkg.origin import (  # noqa: E402
    populate_app_from_origin,
    validate_origin_health,
    validate_update_health,
)
from pkg.updates import (  # noqa: E402
    _check_update,
    _git_origin_candidate,
    _load_update_state,
    _next_version_identity,
    _prepare_update,
    _toml_value,
    _update_paths,
    _write_update_state,
)
from pkg.windows import (  # noqa: E402
    is_current_user_admin,
    wait_for_keypress,
)

__version__ = "0.12.0"
__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4


EXTENDED_HELP = r"""
Extended help
-------------

Quick start
~~~~~~~~~~~

Notes:
  - ``pkg --help`` and ``pkg --version`` do not write files.
  - ``install`` does not auto-create ``pkg.toml``.
  - ``config check`` validates ``pkg.toml`` and origin script references without writing files.
  - ``upgrade check`` discovers an update without downloading it.
  - ``upgrade download`` stages an available update without activating it.
  - ``upgrade install`` activates the most recently downloaded update.
  - ``pkg.local`` hooks do not install missing dependencies unless
    ``--local-deps-autoinstall`` is supplied.
  - ``config update`` creates a documented starter template when ``pkg.toml`` is missing.
  - ``config from-legacy`` builds canonical ``pkg.toml`` from legacy package files.
  - Contributor notes live in ``docs/development_guide.md``.

Run the tool from inside a *version directory*:

  pkg install         # installs from the current working directory

Or pass a path to a version directory:

  pkg install C:\opt\pkgs\Ripgrep\v14.1.0.l1

You may also pass the *package root* (the directory that contains ``current``);
in that case the tool installs from the ``current`` junction:

  pkg install C:\opt\pkgs\Ripgrep

If ``current`` is missing, a package root with exactly one version directory
is still accepted and the tool uses that version directory directly.

Scopes
~~~~~~

User scope:
  - shortcuts: %APPDATA%\Microsoft\Windows\Start Menu\opt
  - PATH/env:  HKCU\Environment
  - bin dir:   %USERPROFILE%\bin

Machine scope (requires admin):
  - shortcuts: %PROGRAMDATA%\Microsoft\Windows\Start Menu\opt
  - PATH/env:  HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment
  - bin dir:   <SYSTEMDRIVE>\bin

Canonical config keys
~~~~~~~~~~~~~~~~~~~~~

1) Origin (optional ``origin`` table)
   A package can populate a missing or empty ``App/`` before components are
   installed. Built-in zip origins use:

     [origin]
     url = "https://example.invalid/tool.zip"
     checksum = "sha256:<64 hex characters>"
     extractSubdir = "tool"

   Historical origins use repeated versioned tables. Entries may contain only
   a version until you try to install from that entry:

     [[origin.versions]]
     version = "1.1.0"
     url = "https://example.invalid/tool-1.1.0.zip"

   If the current origin is one of those entries, omit top-level ``url`` and
   ``script``. ``pkg`` selects the entry matching the package top-level
   ``version``:

     [origin]

     [[origin.versions]]
     version = "1.1.0"
     url = "https://example.invalid/tool-1.1.0.zip"

   Script origins use:

     [origin]
     script = "scripts\populate-app.ps1"

   ``script`` and ``url`` are mutually exclusive. Use ``--refresh-app`` to
   replace an already populated ``App/``. Use ``--no-checksum`` to skip a
   configured checksum with a warning.

2) Shortcuts (``shortcut`` list)
   Supported keys:

   - ``name`` (required): output name after expansion; may be a simple name,
     a nested relative path under the default shortcut root, or a path-like
     destination that resolves outside that root
   - ``targetPath`` (required): executable path
   - ``arguments`` (optional)
   - ``workingDirectory`` (optional)
   - ``iconLocation`` (optional): e.g. ``C:\path\icon.ico,0``
   - ``description`` (optional)

3) Environment variables (``environment`` list)
   Canonical keys are ``Name`` and ``Value``.

4) PATH additions (``path`` list)
   Use repeated ``[[path]]`` tables with the single key ``value``.

5) Bin wrappers (``bin`` list)
   Each entry has ``name`` and ``content``. ``name`` follows the same output
   placement rule as shortcuts: it may be a simple name, a nested relative
   path, or a path-like destination outside the default bin root. Wrapper
   content uses the script expansion mode so PowerShell variables such as
   ``$PSScriptRoot`` remain literal unless they are package variables.

Output placement notes
~~~~~~~~~~~~~~~~~~~~~~

- ``shortcut.name`` and ``bin.name`` are expanded before placement.
- Nested relative paths inside the default scope root are allowed.
- Absolute paths and escaping parent traversal are also allowed.
- When the final destination lands outside the default shortcut/bin root,
  install prints a warning but still creates the output.

Variable expansion
~~~~~~~~~~~~~~~~~~

- ``$App``, ``$Icons``, and ``$Shortcuts`` expand everywhere.
- ``${VAR}`` expands everywhere and must resolve.
- plain non-package ``$NAME`` tokens are treated as unresolved in regular
  fields and remain literal inside wrapper content.
"""


def print_action_banner(operation: str, scope: Scope) -> None:
    """Emit the standard CLI banner for one operation.

    Parameters
    ----------
    operation : str
        Command currently being executed.
    scope : Scope
        Installation scope selected by the caller.

    """
    log_info("")
    log_info("=" * 60)
    log_info("gurlatsev/pkg: Package Manager")
    log_info(f"Operation: {operation}")
    log_info(f"Scope: {scope.value}")
    log_info("=" * 60)
    log_info("")


def action_failure(
    message: str, *, exit_code: int, warnings: Optional[List[str]] = None
) -> ActionResult:
    """Create a failed action result and report the error.

    Parameters
    ----------
    message : str
        Human-readable error message.
    exit_code : int
        Exit code that should be returned to the caller.
    warnings : Optional[List[str]]
        Optional list of already-collected warnings.

    Returns
    -------
    ActionResult
        An :class:`ActionResult` representing the failure.

    """
    log_error(message)
    return ActionResult(
        ok=False,
        changed=False,
        warnings=warnings or [],
        errors=[message],
        exit_code=exit_code,
    )


def _is_update_bootstrap(
    identity: PackageIdentity, config: dict
) -> bool:
    """Return whether a template should stage its first immutable version."""
    origin = config.get("origin")
    update = config.get("update")
    is_bootstrap = identity.version.startswith("bootstrap")
    git_bootstrap = (
        is_bootstrap
        and origin is not None
        and origin.get("mode") == "git"
        and update is not None
        and update["check"]["mode"] == "git"
        and update["payload"]["mode"] == "git"
    )
    release_bootstrap = (
        is_bootstrap
        and update is not None
        and update["check"]["mode"] in {"github", "module"}
        and update["payload"]["mode"] in {"zip", "module"}
    )
    return bool(git_bootstrap or release_bootstrap)


def check_package_update(
    package_path: Path, *, local_deps_autoinstall: bool = False
) -> ActionResult:
    """Check one package's configured source and persist its result.

    Parameters
    ----------
    package_path : Path
        Package root or ``current`` junction whose update source should be
        checked, or a supported bootstrap template version.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.

    Returns
    -------
    ActionResult
        Check outcome with warnings and the recommended process exit code.
    """

    # Update actions require the active package view because update state is
    # owned by the package root, not by an arbitrary historical version.
    identity, from_current = resolve_input_path(package_path)
    config, _, warnings = read_runtime_config(identity, use_defaults=False)
    if config.get("update") is None:
        return ActionResult(
            True, warnings=warnings + ["Updates are not configured for this package"]
        )
    bootstrap = not from_current and _is_update_bootstrap(identity, config)
    if not from_current and not identity.is_current and not bootstrap:
        return action_failure(
            "Update actions require the active version, package root, or "
            "current junction, except for a supported bootstrap template",
            exit_code=EXIT_USER_ERROR,
            warnings=warnings,
        )

    # Acquire the package-root update lock before creating work files so
    # concurrent checks and updates cannot overwrite each other's state.
    paths = _update_paths(identity.package_root)
    paths["locks"].mkdir(parents=True, exist_ok=True)
    lock = paths["locks"] / "update.toml"
    try:
        with open(lock, "x", encoding="utf-8") as handle:
            handle.write(f"pid = {os.getpid()}\n")
    except FileExistsError:
        return action_failure(
            f"An update operation is already active: {lock}",
            exit_code=EXIT_MUTATION_ERROR,
            warnings=warnings,
        )

    # Give hooks an isolated work directory and always remove both work and
    # lock state, regardless of whether discovery succeeds.
    work = paths["work"] / str(uuid.uuid4())
    work.mkdir(parents=True)
    (work / "pycache").mkdir()
    try:
        # Persist timing and candidate identity only after the source check
        # returns a normalized result.
        state = _load_update_state(paths["state"])
        state["lastAttemptedCheck"] = datetime.now(timezone.utc).isoformat()
        if bootstrap and config["update"]["check"]["mode"] == "git":
            status = "available"
            candidate = _git_origin_candidate(identity, config, state)
        else:
            status, candidate = _check_update(
                identity,
                config,
                state,
                work,
                local_deps_autoinstall=local_deps_autoinstall,
            )
        state.update(
            {
                "lastSuccessfulCheck": datetime.now(timezone.utc).isoformat(),
                "lastStatus": status,
                "lastCandidateId": candidate["candidateId"]
                if candidate
                else state.get("lastCandidateId"),
                "lastError": None,
            }
        )
        _write_update_state(paths["state"], state)
        if candidate:
            log_info(f"Available: v{candidate['version']} ({candidate['candidateId']})")
        else:
            log_info(f"Current: {identity.version_string}")
        return ActionResult(True, warnings=warnings, changed=False)
    except (ConfigValidationError, ValueError) as exc:
        return action_failure(
            str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings
        )
    except Exception as exc:
        return action_failure(
            str(exc), exit_code=EXIT_MUTATION_ERROR, warnings=warnings
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.unlink(missing_ok=True)


def download_package_update(
    package_path: Path,
    *,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
) -> ActionResult:
    """Check, download, and stage an available package update.

    Parameters
    ----------
    package_path : Path
        Package root or ``current`` junction whose active version should be
        updated, or a supported bootstrap template version.
    no_checksum : bool, default=False
        Whether checksum verification may be bypassed for downloaded payloads.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.

    Returns
    -------
    ActionResult
        Download outcome with warnings and the recommended process exit code.
    """

    # Resolve update ownership and policy before creating manager state.
    identity, from_current = resolve_input_path(package_path)
    config, _, warnings = read_runtime_config(identity, use_defaults=False)
    update = config.get("update")
    if update is None:
        return ActionResult(
            True, warnings=warnings + ["Updates are not configured for this package"]
        )
    bootstrap = not from_current and _is_update_bootstrap(identity, config)
    if not from_current and not identity.is_current and not bootstrap:
        return action_failure(
            "Update actions require the active version, package root, or "
            "current junction, except for a supported bootstrap template",
            exit_code=EXIT_USER_ERROR,
            warnings=warnings,
        )

    # Serialize discovery and staging beneath one package-root lock.
    paths = _update_paths(identity.package_root)
    paths["locks"].mkdir(parents=True, exist_ok=True)
    lock = paths["locks"] / "update.toml"
    try:
        lock.open("x").close()
    except FileExistsError:
        return action_failure(
            f"An update operation is already active: {lock}",
            exit_code=EXIT_MUTATION_ERROR,
            warnings=warnings,
        )

    # Keep downloads, hook caches, and staged trees in disposable work.
    work = paths["work"] / str(uuid.uuid4())
    work.mkdir(parents=True)
    (work / "pycache").mkdir()
    try:
        # Every explicit download contacts the configured source and records its
        # resulting candidate before deciding whether a payload is needed.
        state = _load_update_state(paths["state"])
        if bootstrap and update["check"]["mode"] == "git":
            status = "available"
            candidate = _git_origin_candidate(identity, config, state)
        else:
            status, candidate = _check_update(
                identity,
                config,
                state,
                work,
                local_deps_autoinstall=local_deps_autoinstall,
            )
        state.update(
            {
                "lastSuccessfulCheck": datetime.now(timezone.utc).isoformat(),
                "lastStatus": status,
                "lastCandidateId": candidate["candidateId"]
                if candidate
                else state.get("lastCandidateId"),
            }
        )
        _write_update_state(paths["state"], state)
        if status == "current" or candidate is None:
            return ActionResult(True, warnings=warnings)

        # Reuse an already committed candidate or atomically commit a complete
        # staged version. Activation is a separate explicit command.
        new_identity = _next_version_identity(identity, candidate)
        receipt = paths["receipts"] / f"{new_identity.version_string}.toml"
        if new_identity.version_path.exists():
            log_info(f"Downloaded: {new_identity.version_string}")
            return ActionResult(True, warnings=warnings)
        staged = _prepare_update(
            identity,
            config,
            candidate,
            work,
            no_checksum=no_checksum,
            local_deps_autoinstall=local_deps_autoinstall,
        )
        os.replace(work / "version", staged.version_path)
        paths["receipts"].mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            receipt,
            f"schemaVersion = 1\ncandidateId = {_toml_value(candidate['candidateId'])}\nversion = {_toml_value(staged.version)}\nlocalVersion = {staged.local_version}\n",
        )
        log_info(f"Downloaded: {staged.version_string}")
        return ActionResult(True, changed=True, warnings=warnings)
    except (ConfigValidationError, ValueError) as exc:
        return action_failure(
            str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings
        )
    except Exception as exc:
        return action_failure(
            str(exc), exit_code=EXIT_MUTATION_ERROR, warnings=warnings
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.unlink(missing_ok=True)


def install_downloaded_update(
    package_path: Path, *, scope: Scope = Scope.AUTO, no_checksum: bool = False
) -> ActionResult:
    """Activate the most recently downloaded update for a package.

    Parameters
    ----------
    package_path : Path
        Package root or active package path that owns staged updates.
    scope : Scope, default=Scope.AUTO
        Installation scope used to activate the staged version.
    no_checksum : bool, default=False
        Accepted for command consistency; checksums are verified during download.

    Returns
    -------
    ActionResult
        Activation outcome with warnings and the recommended process exit code.
    """
    _ = no_checksum

    # Resolve the active package and inspect only manager-owned receipts before
    # selecting an immutable version to activate.
    try:
        identity, _ = resolve_input_path(package_path)
    except ValueError as exc:
        return action_failure(str(exc), exit_code=EXIT_USER_ERROR)
    receipts = _update_paths(identity.package_root)["receipts"]
    receipt_paths = sorted(
        receipts.glob("v*.l*.toml"), key=lambda path: path.stat().st_mtime, reverse=True
    ) if receipts.exists() else []
    if not receipt_paths:
        return action_failure(
            "No downloaded update is available. Run 'pkg upgrade download' first.",
            exit_code=EXIT_USER_ERROR,
        )

    # Receipts name the committed version and are validated against the package
    # layout before the regular install workflow manages the current junction.
    try:
        receipt = read_toml_file(receipt_paths[0])
        version = receipt.get("version")
        local_version = receipt.get("localVersion")
        if not isinstance(version, str) or not isinstance(local_version, int):
            raise ValueError("receipt has invalid version metadata")
        version_path = identity.package_root / f"v{version}.l{local_version}"
        if not version_path.is_dir():
            raise ValueError(f"downloaded version is missing: {version_path}")
    except (OSError, ValueError) as exc:
        return action_failure(
            f"Cannot activate downloaded update: {exc}", exit_code=EXIT_USER_ERROR
        )
    return install_package(version_path, scope=scope)


def install_package(
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    use_defaults: bool = False,
    allow_downgrade: bool = False,
    refresh_app: bool = False,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
) -> ActionResult:
    """Install or reinstall a package and return a truthful action result.

    Same-version installs are intentionally not treated as a no-op. Once the
    selected version is allowed to proceed, the fixed component sequence reruns so
    broken shortcuts, environment variables, PATH entries, and wrapper files
    can be restored. Depending on *package_path*, reinstall may also refresh
    the ``current`` junction.

    Parameters
    ----------
    package_path : Path
        User-supplied path to a version directory, package root, or
        ``current`` junction.
    scope : Scope, default=Scope.AUTO
        Installation scope to use for mutations. Automatic selection uses
        machine scope for administrators unless the package is portable-only.
    use_defaults : bool, default=False
        Whether installs may fall back to runtime defaults when TOML loading
        fails.
    allow_downgrade : bool, default=False
        Whether installs may replace ``current`` even when it already points to
        a newer version. Ordinary same-version repair reruns do not require
        this override.
    refresh_app : bool, default=False
        Whether to repopulate ``App/`` from origin even when it already
        contains files.
    no_checksum : bool, default=False
        Whether to skip configured origin checksum verification.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies
        while promoting a bootstrap package.

    Returns
    -------
    ActionResult
        Truthful description of the install outcome and recommended exit code.

    """
    print_action_banner("install", scope)

    # Resolve the caller's path first so every later step works from a concrete
    # version directory and knows whether ``current`` was the original target.
    try:
        identity, installing_from_current = resolve_input_path(Path(package_path))
    except ValueError as exc:
        return action_failure(str(exc), exit_code=EXIT_USER_ERROR)

    # Load runtime config before any mutations so validation failures stop the
    # install before automatic scope selection or filesystem work.
    try:
        runtime_config, raw_config_data, load_warnings = read_runtime_config(
            identity, use_defaults=use_defaults
        )
    except (ConfigValidationError, RuntimeError, ValueError) as exc:
        return action_failure(
            f"Failed to load package metadata/config: {exc}", exit_code=EXIT_USER_ERROR
        )
    except OSError as exc:
        return action_failure(
            f"Failed to load package metadata/config: {exc}",
            exit_code=EXIT_MUTATION_ERROR,
        )

    warnings = list(load_warnings)
    for warning in load_warnings:
        log_warning(warning)

    # Keep directory-derived metadata authoritative. Installation never rewrites
    # package definitions; authors must run `pkg config update` explicitly.
    inconsistencies = check_metadata_consistency(identity, raw_config_data)
    if inconsistencies:
        log_error("Configuration inconsistencies detected:")
        for message in inconsistencies:
            log_error(f"  - {message}")
        log_info("Run this command before installing:")
        log_info(f"  pkg config update {identity.version_path}")
        return ActionResult(
            ok=False,
            changed=False,
            warnings=warnings,
            errors=inconsistencies,
            exit_code=EXIT_USER_ERROR,
        )

    log_info(f"Package: {identity.name}")
    log_info(f"Version: {identity.version_string}")
    log_info(f"Path: {identity.version_path}")
    log_info(f"only_portable: {runtime_config['only_portable']}")
    log_info("")

    # Resolve automatic scope only after reading the portability policy.
    # Administrators use machine scope when permitted; every other automatic
    # installation remains per-user.
    scope_was_auto = scope == Scope.AUTO
    auto_admin = False
    if scope_was_auto:
        auto_admin = is_current_user_admin()
        scope = (
            Scope.MACHINE
            if auto_admin and not runtime_config["only_portable"]
            else Scope.USER
        )
        log_info(f"Selected scope: {scope.value}")
        log_info("")

    # Reject explicit scope combinations the package model cannot support
    # before any junction, origin, or scope-specific filesystem work begins.
    if runtime_config["only_portable"] and scope == Scope.MACHINE:
        return action_failure(
            "only_portable packages cannot be installed system-wide. Please use User scope.",
            exit_code=EXIT_USER_ERROR,
            warnings=warnings,
        )

    if scope == Scope.MACHINE and not (auto_admin or is_current_user_admin()):
        return action_failure(
            "Machine scope requires administrator privileges. Please run as administrator.",
            exit_code=EXIT_USER_ERROR,
            warnings=warnings,
        )

    try:
        scope_paths = compute_scope_paths(scope)
    except (RuntimeError, ValueError, OSError) as exc:
        return action_failure(
            f"Failed to resolve {scope.value} scope paths: {exc}",
            exit_code=EXIT_MUTATION_ERROR,
            warnings=warnings,
        )

    # Bootstrap version strings are templates, never installed versions. Let
    # their generic Git or package-local module check stage the first immutable
    # version before junction, origin, or component work begins.
    if _is_update_bootstrap(identity, runtime_config):
        log_info("Promoting bootstrap into an immutable package version...")
        download_result = download_package_update(
            identity.version_path,
            no_checksum=no_checksum,
            local_deps_autoinstall=local_deps_autoinstall,
        )
        if not download_result.ok:
            return download_result
        result = install_downloaded_update(identity.version_path, scope=scope)
        result.warnings = warnings + result.warnings
        return result

    # Update the package-root ``current`` junction unless the caller already
    # targeted it directly. Older installed versions are left intact unless the
    # caller explicitly forces replacement.
    junction_changed = False
    if installing_from_current:
        log_info(
            "Installing from resolved 'current' target (skipping junction management)"
        )
    else:
        log_info("Managing 'current' junction...")
        try:
            junction_changed = update_current_junction_if_needed(
                identity, allow_downgrade=allow_downgrade
            )
        except ValueError as exc:
            return action_failure(
                str(exc), exit_code=EXIT_USER_ERROR, warnings=warnings
            )
        except Exception as exc:
            return action_failure(
                str(exc), exit_code=EXIT_MUTATION_ERROR, warnings=warnings
            )

        if not junction_changed and not identity.is_current:
            log_info(
                "Skipping component installation (newer version already installed)"
            )
            return ActionResult(
                ok=True,
                changed=False,
                warnings=warnings,
                exit_code=EXIT_SUCCESS,
            )

    # Populate ``App/`` before installing shortcuts or wrappers so every later
    # artifact can rely on the application payload being present.
    log_info("")
    origin_result = populate_app_from_origin(
        identity,
        runtime_config,
        no_checksum=no_checksum,
        refresh_app=refresh_app,
    )
    warnings.extend(origin_result.warnings)
    if not origin_result.ok:
        log_error("Origin population failed:")
        for error in origin_result.errors:
            log_error(f"  - {error}")
        return ActionResult(
            ok=False,
            changed=junction_changed or origin_result.changed,
            warnings=warnings,
            errors=origin_result.errors,
            exit_code=EXIT_MUTATION_ERROR,
        )

    # Apply the fixed component sequence only after origin population succeeds.
    log_info("")
    log_info("Installing components...")
    component_result = install_components(identity, scope, scope_paths, runtime_config)
    warnings.extend(component_result.warnings)

    if not component_result.ok:
        log_error("One or more install steps failed:")
        for error in component_result.errors:
            log_error(f"  - {error}")
        return ActionResult(
            ok=False,
            changed=junction_changed or component_result.changed,
            warnings=warnings,
            errors=component_result.errors,
            exit_code=EXIT_MUTATION_ERROR,
        )

    return ActionResult(
        ok=True,
        changed=junction_changed or origin_result.changed or component_result.changed,
        warnings=warnings,
        exit_code=EXIT_SUCCESS,
    )
def update_package_config(
    package_path: Path, *, scope: Scope = Scope.USER
) -> ActionResult:
    """Synchronize ``pkg.toml`` metadata for one package.

    Parameters
    ----------
    package_path : Path
        User-supplied path to a version directory, package root, or
        ``current`` junction.
    scope : Scope, default=Scope.USER
        Selected CLI scope, used only for standard banner output.

    Returns
    -------
    ActionResult
        Metadata update outcome and recommended exit code.

    """
    print_action_banner("config update", scope)

    try:
        identity, _ = resolve_input_path(Path(package_path))
    except ValueError as exc:
        return action_failure(str(exc), exit_code=EXIT_USER_ERROR)

    try:
        step_result = update_config_file(identity)
    except (ConfigValidationError, RuntimeError, ValueError) as exc:
        return action_failure(
            f"Failed to update configuration: {exc}", exit_code=EXIT_USER_ERROR
        )
    except OSError as exc:
        return action_failure(
            f"Failed to update configuration: {exc}", exit_code=EXIT_MUTATION_ERROR
        )

    return ActionResult(
        ok=step_result.ok,
        changed=step_result.changed,
        warnings=step_result.warnings,
        errors=step_result.errors,
        exit_code=EXIT_SUCCESS if step_result.ok else EXIT_MUTATION_ERROR,
    )


def convert_legacy_config(
    package_path: Path,
    *,
    output_path: Optional[Path] = None,
    dry_run: bool = False,
) -> ActionResult:
    """Convert legacy package files into canonical ``pkg.toml``.

    Parameters
    ----------
    package_path : Path
        Directory containing legacy package files.
    output_path : Path | None, default=None
        Destination TOML path. The default is ``pkg.toml`` inside the legacy
        package directory.
    dry_run : bool, default=False
        Whether to print canonical TOML without writing or backing up files.

    Returns
    -------
    ActionResult
        Conversion outcome and recommended process exit code.
    """

    # Legacy source directories may predate the version-directory layout, so
    # validate them independently of normal package identity resolution.
    base_dir = Path(package_path).resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        return action_failure(
            f"Legacy package directory does not exist: {base_dir}",
            exit_code=EXIT_USER_ERROR,
        )

    # Default output belongs to the converted directory. Explicit relative
    # paths remain relative to the caller's working directory.
    destination = (
        base_dir / "pkg.toml"
        if output_path is None
        else Path(output_path).expanduser().resolve()
    )

    try:
        changed = convert_legacy_directory(
            base_dir,
            destination,
            dry_run=dry_run,
        )
    except (TypeError, ValueError) as exc:
        return action_failure(str(exc), exit_code=EXIT_USER_ERROR)
    except OSError as exc:
        return action_failure(str(exc), exit_code=EXIT_MUTATION_ERROR)

    return ActionResult(ok=True, changed=changed, exit_code=EXIT_SUCCESS)


def health_check_package(
    package_path: Path, *, scope: Scope = Scope.USER
) -> ActionResult:
    """Validate one package configuration without mutating state.

    Parameters
    ----------
    package_path : Path
        User-supplied path to a version directory, package root, or
        ``current`` junction.
    scope : Scope, default=Scope.USER
        Selected CLI scope, used only for standard banner output.

    Returns
    -------
    ActionResult
        Validation outcome and recommended exit code.

    """
    print_action_banner("config check", scope)

    try:
        identity, _ = resolve_input_path(Path(package_path))
    except ValueError as exc:
        return action_failure(str(exc), exit_code=EXIT_USER_ERROR)

    try:
        runtime_config, raw_config_data, load_warnings = read_runtime_config(
            identity, use_defaults=False
        )
    except (ConfigValidationError, RuntimeError, ValueError) as exc:
        return action_failure(
            f"Failed to load package metadata/config: {exc}", exit_code=EXIT_USER_ERROR
        )
    except OSError as exc:
        return action_failure(
            f"Failed to load package metadata/config: {exc}",
            exit_code=EXIT_MUTATION_ERROR,
        )

    warnings = list(load_warnings)
    for warning in load_warnings:
        log_warning(warning)

    errors = check_metadata_consistency(identity, raw_config_data)
    errors.extend(validate_origin_health(identity, runtime_config.get("origin")))
    errors.extend(validate_update_health(identity, runtime_config.get("update")))
    if errors:
        log_error("Health check failed:")
        for error in errors:
            log_error(f"  - {error}")
        return ActionResult(
            ok=False,
            changed=False,
            warnings=warnings,
            errors=errors,
            exit_code=EXIT_USER_ERROR,
        )

    log_info(f"Package: {identity.name}")
    log_info(f"Version: {identity.version_string}")
    log_info(f"Path: {identity.version_path}")
    log_info("Health check passed.")
    return ActionResult(
        ok=True, changed=False, warnings=warnings, exit_code=EXIT_SUCCESS
    )


class _ExtendedHelpAction(argparse.Action):
    """Argparse action that prints standard help plus extended documentation."""

    def __init__(
        self,
        option_strings,
        dest=argparse.SUPPRESS,
        default=argparse.SUPPRESS,
        help=None,
    ):
        """Create the extended-help argparse action.

        Parameters
        ----------
        option_strings : list[str]
            CLI flags that trigger the action.
        dest : str, default=argparse.SUPPRESS
            Argparse destination name.
        default : Any, default=argparse.SUPPRESS
            Default argparse value.
        help : str | None, default=None
            Help text shown in ``--help`` output.

        """
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        """Print standard and extended help, then exit.

        Parameters
        ----------
        parser : argparse.ArgumentParser
            Active argument parser.
        namespace : argparse.Namespace
            Parsed namespace, unused by this action.
        values : Any
            Parsed values for the option, unused by this action.
        option_string : str | None, default=None
            Exact CLI option that triggered the action.

        """
        _ = namespace
        _ = values
        _ = option_string
        parser.print_help()
        log_info("")
        log_info(EXTENDED_HELP.strip())
        log_info("")
        parser.exit()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for ``pkg``.

    Parameters
    ----------
    argv : list[str] | None, default=None
        Optional argument list excluding the program name. When omitted,
        :data:`sys.argv[1:]` is used by :mod:`argparse`.

    Returns
    -------
    int
        Process exit code.

    """
    parser = argparse.ArgumentParser(
        description="Local Package Manager for Windows (gurlatsev/pkg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s install                    # Install the current package\n"
            "  %(prog)s upgrade check C:\\Packages\\Tool\n"
            "  %(prog)s upgrade download C:\\Packages\\Tool\n"
            "  %(prog)s upgrade install C:\\Packages\\Tool\n"
            "  %(prog)s config check C:\\Packages\\Tool\n"
            "  %(prog)s config update C:\\Packages\\Tool\n"
            "  %(prog)s config from-legacy C:\\OldPackages\\Tool\n"
            "  %(prog)s tui                        # Open the interactive interface\n"
            "  %(prog)s --help-extended          # Show detailed help and config examples\n"
        ),
    )

    parser.add_argument(
        "--help-extended",
        action=_ExtendedHelpAction,
        help="Show standard help plus extended documentation and examples",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )
    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in Scope],
        default=Scope.AUTO.value,
        help="Installation scope (default: Auto; administrators use Machine unless portable-only)",
    )

    parser.add_argument(
        "--pause",
        action="store_true",
        help="Pause for a keypress before exiting",
    )

    parser.add_argument(
        "--use-defaults",
        action="store_true",
        default=False,
        help="Proceed with defaults if pkg.toml exists but cannot be parsed/validated",
    )

    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        default=False,
        help="Allow install to replace current even if it already targets a newer version",
    )

    parser.add_argument(
        "--refresh-app",
        action="store_true",
        default=False,
        help="Clear and repopulate App from [origin] before installing components",
    )

    parser.add_argument(
        "--no-checksum",
        action="store_true",
        default=False,
        help="Skip [origin].checksum verification with a warning",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output path for config from-legacy (default: <path>/pkg.toml)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print config from-legacy TOML without writing files",
    )

    parser.add_argument(
        "--toml",
        action="store_true",
        default=False,
        help="Emit a machine-readable command summary",
    )

    parser.add_argument(
        "--local-deps-autoinstall",
        action="store_true",
        default=False,
        help="Allow trusted pkg.local update hooks to install missing dependencies",
    )

    parser.add_argument(
        "command",
        nargs="?",
        choices=["install", "upgrade", "config", "tui"],
        default="install",
        help="Top-level command (default: install)",
    )
    parser.add_argument(
        "operation",
        nargs="?",
        help="upgrade: check, download, or install; config: from-legacy, update, or check",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Package path (default: current directory)",
    )

    args = parser.parse_args(argv)
    if args.command == "tui":
        if args.operation is not None or args.path is not None:
            parser.error("tui does not accept an operation or package path")
        try:
            # Provision every declared dependency for this pkg-owned feature
            # before importing its optional interface module.
            from pkg.dependencies import ensure_runtime_dependencies

            ensure_runtime_dependencies("tui")
            from pkg.tui import run_tui

            return run_tui()
        except RuntimeError as install_error:
            log_error(f"Could not install a TUI dependency: {install_error}")
            return EXIT_MUTATION_ERROR

    scope = Scope(args.scope)
    result: ActionResult
    label = args.command

    try:
        # Install has no subcommand, so its second positional value is its path.
        if args.command == "install":
            if args.path is not None:
                parser.error("install accepts one optional package path")
            package_path = Path(args.operation or ".").expanduser()
            label = "install"
            result = install_package(
                package_path,
                scope=scope,
                use_defaults=args.use_defaults,
                allow_downgrade=args.allow_downgrade,
                refresh_app=args.refresh_app,
                no_checksum=args.no_checksum,
                local_deps_autoinstall=args.local_deps_autoinstall,
            )
        else:
            operations = {
                "upgrade": {"check", "download", "install"},
                "config": {"from-legacy", "update", "check"},
            }
            if args.operation not in operations[args.command]:
                parser.error(
                    f"{args.command} requires one of: "
                    + ", ".join(sorted(operations[args.command]))
                )
            package_path = Path(args.path or ".").expanduser()
            label = f"{args.command} {args.operation}"
            if args.command == "upgrade" and args.operation == "check":
                result = check_package_update(
                    package_path,
                    local_deps_autoinstall=args.local_deps_autoinstall,
                )
            elif args.command == "upgrade" and args.operation == "download":
                result = download_package_update(
                    package_path,
                    no_checksum=args.no_checksum,
                    local_deps_autoinstall=args.local_deps_autoinstall,
                )
            elif args.command == "upgrade":
                result = install_downloaded_update(
                    package_path, scope=scope, no_checksum=args.no_checksum
                )
            elif args.operation == "update":
                result = update_package_config(package_path, scope=scope)
            elif args.operation == "from-legacy":
                output_path = Path(args.output).expanduser() if args.output else None
                result = convert_legacy_config(
                    package_path,
                    output_path=output_path,
                    dry_run=args.dry_run,
                )
            else:
                result = health_check_package(package_path, scope=scope)
    except Exception as exc:
        message = f"Unexpected internal error: {exc}"
        log_error(message)
        result = ActionResult(ok=False, errors=[message], exit_code=EXIT_INTERNAL_ERROR)

    # Dry-run conversion is designed for redirection and TOML parsing, so keep
    # stdout free of the normal action footer and pause prompt.
    if label == "config from-legacy" and args.dry_run:
        return result.exit_code

    if args.pause:
        log_info("")
        log_info("Press any key to continue...")
        wait_for_keypress()

    if args.toml:
        print(f"ok = {'true' if result.ok else 'false'}")
        print(f"changed = {'true' if result.changed else 'false'}")
        print(
            f"status = {_toml_value('updated' if result.changed else ('failed' if not result.ok else 'current'))}"
        )
    log_info("")
    log_info("-" * 60)
    if result.ok and result.changed:
        log_info(f"{label} completed successfully.")
    elif result.ok:
        log_info(f"{label} completed successfully (no changes needed).")
    else:
        log_info(f"{label} failed.")
    log_info("-" * 60)
    return result.exit_code


# ------------------------------------------
# Section: Script entry point
# ------------------------------------------
if __name__ == "__main__":
    raise SystemExit(main())
