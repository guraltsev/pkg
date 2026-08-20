#!/usr/bin/env python3
"""Install and maintain local Windows packages declared by ``pkg.toml``.

The module is the stable executable and Python facade for package actions. It
coordinates configuration, origin population, component installation, and
updates while focused implementation domains live in the ``gupkg`` package.

Usage and API
-------------
Call ``main(...)`` for command-line execution. Embedders may call
``install_package(...)``, ``update_package_config(...)``,
``convert_legacy_config(...)``, ``health_check_package(...)``,
``check_package_update(...)``, ``download_package_update(...)``, or
``install_downloaded_update(...)``, or ``full_package_upgrade(...)`` directly.

Implementation Approach
-----------------------
The facade resolves each action into one top-level workflow and delegates
platform, parsing, payload, and staging mechanics to focused runtime modules.
Directory identity and result objects cross those boundaries explicitly.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Direct script execution starts with ``src/gupkg`` on sys.path. Add ``src`` so
# the facade resolves this package by its canonical name without changing the
# caller's working directory.
_SRC_ROOT = Path(__file__).resolve().parent.parent
_src_root_text = str(_SRC_ROOT)
if _src_root_text in sys.path:
    sys.path.remove(_src_root_text)
sys.path.insert(0, _src_root_text)

from gupkg.components import install_components  # noqa: E402
from gupkg.configuration import (  # noqa: E402
    check_metadata_consistency,
    read_runtime_config,
)
from gupkg.core import (  # noqa: E402
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    compare_package_versions,
    is_version_directory_name,
    log_error,
    log_info,
    log_warning,
    read_toml_file,
    write_text_atomic,
)
from gupkg.layout import (  # noqa: E402
    compute_scope_paths,
    resolve_input_path,
    update_current_junction_if_needed,
)
from gupkg.legacy_to_gupkg_toml import convert_legacy_directory  # noqa: E402
from gupkg.metadata import update_config_file  # noqa: E402
from gupkg.origin import (  # noqa: E402
    app_has_payload,
    populate_app_from_origin,
    validate_origin_health,
    validate_update_health,
)
from gupkg.updates import (  # noqa: E402
    _check_update,
    _git_origin_candidate,
    _load_update_state,
    _next_version_identity,
    _prepare_update,
    _toml_value,
    _update_paths,
    _write_update_state,
)
from gupkg.manager import (  # noqa: E402
    discover_manager,
    load_manager_config,
    scope_id,
    scope_name,
    select_target,
)
from gupkg.windows import (  # noqa: E402
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
  - ``gupkg --help`` and ``gupkg --version`` do not write files.
  - ``install`` does not auto-create ``pkg.toml``.
  - ``config check`` validates ``pkg.toml`` and origin script references without writing files.
  - ``upgrade check`` discovers an update without downloading it.
  - ``upgrade download`` stages an available update without activating it.
  - ``upgrade install`` activates the most recently downloaded update.
  - ``upgrade full`` discovers once, stages, and activates an available update.
  - ``pkg.local`` hooks do not install missing dependencies unless
    ``--local-deps-autoinstall`` is supplied.
  - ``config update`` creates a documented starter template when ``pkg.toml`` is missing.
  - ``config from-legacy`` builds canonical ``pkg.toml`` from legacy package files.
  - Contributor notes live in ``docs/development_guide.md``.

Run the tool from inside a *version directory*:

  gupkg install         # installs from the current working directory

Or pass a path to a version directory:

  gupkg install C:\opt\gupkgs\Ripgrep\v14.1.0.l1

You may also pass the *package root* (the directory that contains ``current``);
in that case the tool installs from the ``current`` junction:

  gupkg install C:\opt\gupkgs\Ripgrep

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
   Every concrete installed version needs a non-empty ``App/``. A package can
   populate a missing or empty directory before components are installed.
   Built-in zip origins use:

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
   ``script``. ``gupkg`` selects the entry matching the package top-level
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

5) Bin commands (``bin`` list)
   Native shims use ``name`` and ``target`` with optional ``type``,
   ``arguments``, ``forward_args``, ``elevate``, and ``working_dir``. Use
   ``content`` instead of ``target`` only to write a raw command or script file
   when shell behavior is required. Names follow the same placement rule as
   shortcuts. Raw content uses script expansion mode so PowerShell variables
   such as ``$PSScriptRoot`` remain literal unless they are package variables.

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
    log_info("gupkg: Package Manager")
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

    # Any package version may define the update source; persistent update state
    # remains package-owned even when discovery begins from a historical tree.
    identity, _ = resolve_input_path(package_path)
    config, _, warnings = read_runtime_config(identity, use_defaults=False)
    if config.get("update") is None:
        return ActionResult(
            True, warnings=warnings + ["Updates are not configured for this package"]
        )
    bootstrap = _is_update_bootstrap(identity, config)

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
        return ActionResult(True, warnings=warnings, changed=False, status=status)
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
        Package root, ``current`` junction, or version directory whose update
        should be checked and staged.
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
    identity, _ = resolve_input_path(package_path)
    config, _, warnings = read_runtime_config(identity, use_defaults=False)
    update = config.get("update")
    if update is None:
        return ActionResult(
            True, warnings=warnings + ["Updates are not configured for this package"]
        )
    bootstrap = _is_update_bootstrap(identity, config)

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
            return ActionResult(True, warnings=warnings, status="current")

        # Reuse an already committed candidate or atomically commit a complete
        # staged version. Activation is a separate explicit command.
        new_identity = _next_version_identity(identity, candidate)
        receipt = paths["receipts"] / f"{new_identity.version_string}.toml"
        if new_identity.version_path.exists():
            # Reinstalling a bootstrap template must reactivate its original
            # immutable promotion instead of allocating a higher local revision.
            if identity.version.startswith("bootstrap"):
                paths["receipts"].mkdir(parents=True, exist_ok=True)
                write_text_atomic(
                    receipt,
                    f"schemaVersion = 1\ncandidateId = {_toml_value(candidate['candidateId'])}\nversion = {_toml_value(new_identity.version)}\nlocalVersion = {new_identity.local_version}\n",
                )
            log_info(f"Downloaded: {new_identity.version_string}")
            return ActionResult(True, warnings=warnings, status="downloaded")
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
        return ActionResult(
            True, changed=True, warnings=warnings, status="downloaded"
        )
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
        Package root, ``current`` junction, or version directory that owns
        staged updates.
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

    # Resolve the selected package and inspect only manager-owned receipts
    # before selecting an immutable version to activate.
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
            "No downloaded update is available. Run 'gupkg upgrade download' first.",
            exit_code=EXIT_USER_ERROR,
        )

    # The newest receipt identifies the one staged update that may be activated.
    # A receipt for this version or an older one was already consumed or has
    # been superseded, so it must not turn an upgrade command into a reinstall.
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

    if (
        not identity.version.startswith("bootstrap")
        and compare_package_versions(version_path.name, identity.version_string) <= 0
    ):
        return action_failure(
            "No downloaded upgrade is waiting to be installed. Run "
            "'gupkg upgrade download' first.",
            exit_code=EXIT_USER_ERROR,
        )

    # Do not let an older package definition replace a newer installed version.
    newer_versions = sorted(
        path.name
        for path in identity.package_root.iterdir()
        if (
            path.is_dir()
            and is_version_directory_name(path.name)
            and not path.name.startswith("vbootstrap")
            and compare_package_versions(path.name, version_path.name) > 0
        )
    )
    if newer_versions:
        return action_failure(
            "Cannot activate downloaded update because a newer installed version "
            f"exists: {newer_versions[-1]}",
            exit_code=EXIT_USER_ERROR,
        )
    result = install_package(version_path, scope=scope)
    if result.ok:
        receipt_paths[0].unlink(missing_ok=True)
        result.status = "installed-update"
    return result


def full_package_upgrade(
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
) -> ActionResult:
    """Check, stage, and activate an available update in one operation.

    Parameters
    ----------
    package_path : Path
        Package root, ``current`` junction, or version directory whose update
        should be checked, staged, and activated.
    scope : Scope, default=Scope.AUTO
        Installation scope used when activating the staged version.
    no_checksum : bool, default=False
        Whether checksum verification may be bypassed while staging a payload.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.

    Returns
    -------
    ActionResult
        Check, download, or activation outcome with warnings and the
        recommended exit code.
    """

    # Download owns discovery, so a full upgrade contacts the source once and
    # activates only after a complete staged version has been committed.
    download_result = download_package_update(
        package_path,
        no_checksum=no_checksum,
        local_deps_autoinstall=local_deps_autoinstall,
    )
    if not download_result.ok or download_result.status != "downloaded":
        return download_result

    # Resolve the receipt from the original package path; it identifies the
    # newly staged version without requiring the caller to change folders.
    install_result = install_downloaded_update(package_path, scope=scope)
    install_result.warnings = download_result.warnings + install_result.warnings
    return install_result


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
    # package definitions; authors must run `gupkg config update` explicitly.
    inconsistencies = check_metadata_consistency(identity, raw_config_data)
    if inconsistencies:
        log_error("Configuration inconsistencies detected:")
        for message in inconsistencies:
            log_error(f"  - {message}")
        log_info("Run this command before installing:")
        log_info(f"  gupkg config update {identity.version_path}")
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

    # An install is only meaningful for a concrete application payload. Fail
    # before junction mutations when no origin can repair the package.
    if runtime_config.get("origin") is None and not app_has_payload(identity):
        return action_failure(
            "App is missing or empty and no [origin] is configured to populate it",
            exit_code=EXIT_USER_ERROR,
            warnings=warnings,
        )

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
    package_path: Path,
    *,
    scope: Scope = Scope.USER,
    import_shortcuts: bool = True,
) -> ActionResult:
    """Synchronize ``pkg.toml`` metadata for one package.

    Parameters
    ----------
    package_path : Path
        User-supplied path to a version directory, package root, or
        ``current`` junction.
    scope : Scope, default=Scope.USER
        Selected CLI scope, used only for standard banner output.
    import_shortcuts : bool, default=True
        Whether ``.lnk`` files under ``_shortcuts`` are added to the
        configuration and renamed with the ``.lnk.imported`` suffix.

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

    shortcut_changed = False
    shortcuts_dir = identity.version_path / "_shortcuts"
    if import_shortcuts and shortcuts_dir.is_dir():
        shortcut_files = [
            path for path in shortcuts_dir.rglob("*.lnk") if path.is_file()
        ]
        if shortcut_files:
            try:
                # Render the shortcut tables before changing the source files,
                # then consume those files only after the TOML replacement succeeds.
                from gupkg.shortcuts_to_gupkg_toml import (
                    archive_imported_shortcuts,
                    import_shortcuts as import_shortcut_tables,
                )

                rendered, shortcuts = import_shortcut_tables(identity.version_path)
                write_text_atomic(
                    identity.version_path / "pkg.toml", rendered, backup=True
                )
                archive_imported_shortcuts(shortcuts_dir)
                log_info(f"Imported and archived {len(shortcuts)} shortcut(s).")
                shortcut_changed = True
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                return action_failure(
                    f"Failed to import shortcuts: {exc}", exit_code=EXIT_USER_ERROR
                )
            except OSError as exc:
                return action_failure(
                    f"Failed to import shortcuts: {exc}", exit_code=EXIT_MUTATION_ERROR
                )

    return ActionResult(
        ok=step_result.ok,
        changed=step_result.changed or shortcut_changed,
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


def _package_main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for ``gupkg``.

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
        description="Local Package Manager for Windows (gupkg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s install                    # Install the current package\n"
            "  %(prog)s upgrade check C:\\Packages\\Tool\n"
            "  %(prog)s upgrade download C:\\Packages\\Tool\n"
            "  %(prog)s upgrade install C:\\Packages\\Tool\n"
            "  %(prog)s upgrade full C:\\Packages\\Tool\n"
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
        "--import-shortcuts",
        choices=["true", "false"],
        default="true",
        help=(
            "Import and archive .lnk files from _shortcuts during config update "
            "(default: true)"
        ),
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
        help="upgrade: check, download, full, or install; config: from-legacy, update, or check",
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
            # Provision gupkg-owned UI dependencies before importing the
            # optional interface module into the launcher process.
            from gupkg.dependencies import ensure_runtime_dependencies

            ensure_runtime_dependencies("tui")
            from gupkg.tui import run_tui

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
                "upgrade": {"check", "download", "full", "install"},
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
            elif args.command == "upgrade" and args.operation == "full":
                result = full_package_upgrade(
                    package_path,
                    scope=scope,
                    no_checksum=args.no_checksum,
                    local_deps_autoinstall=args.local_deps_autoinstall,
                )
            elif args.command == "upgrade":
                result = install_downloaded_update(
                    package_path, scope=scope, no_checksum=args.no_checksum
                )
            elif args.operation == "update":
                result = update_package_config(
                    package_path,
                    scope=scope,
                    import_shortcuts=args.import_shortcuts == "true",
                )
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
        status = result.status or (
            "updated" if result.changed else ("failed" if not result.ok else "current")
        )
        print(f"status = {_toml_value(status)}")
    log_info("")
    log_info("-" * 60)
    if label == "upgrade check" and result.status == "available":
        log_info(
            "Upgrade is available. Run 'gupkg upgrade download' to stage it; "
            "this check did not change any files."
        )
    elif label == "upgrade check" and result.status == "current":
        log_info("Package is current; this check did not change any files.")
    elif label == "upgrade download" and result.status == "downloaded":
        log_info(
            "Upgrade is downloaded and staged. Run 'gupkg upgrade install' from "
            "this directory or the package root to activate it."
        )
    elif label == "upgrade download" and result.status == "current":
        log_info("Package is current; no upgrade was downloaded.")
    elif label == "upgrade full" and result.status == "current":
        log_info("Package is current; no upgrade was downloaded or activated.")
    elif label == "upgrade full" and result.status == "installed-update":
        log_info("Upgrade was downloaded and is now active.")
    elif label == "upgrade install" and result.status == "installed-update":
        log_info("Downloaded upgrade is now active.")
    elif label == "config check" and result.ok:
        log_info("Configuration is valid; this check did not change any files.")
    elif result.ok and result.changed:
        log_info(f"{label} completed successfully; changes were applied.")
    elif result.ok:
        log_info(f"{label} completed successfully; it made no changes.")
    else:
        log_info(f"{label} failed.")
    log_info("-" * 60)
    return result.exit_code


# ------------------------------------------
# Section: Script entry point
# ------------------------------------------
def _aggregate_check(inventory, *, update: bool, scope: Scope, toml: bool) -> int:
    """Run a read-oriented check for every discovered manifest and summarize it."""
    from gupkg.collection import DiscoveredPackage

    outcomes: list[tuple[DiscoveredPackage, str, ActionResult]] = []
    highest = EXIT_SUCCESS
    for package in inventory.packages:
        # Check each historical manifest independently because update settings
        # may legitimately differ between immutable package versions.
        for manifest in package.manifests:
            result = (
                check_package_update(manifest.version_path)
                if update
                else health_check_package(manifest.version_path, scope=scope)
            )
            outcomes.append((package, manifest.version_path.name, result))
            highest = max(highest, result.exit_code)
    if inventory.diagnostics or any(package.diagnostics for package in inventory.packages):
        highest = max(highest, EXIT_USER_ERROR)

    if toml:
        print("[collection]")
        print(f"root = {_toml_value(str(inventory.root))}")
        print(f"complete = {'true' if inventory.complete else 'false'}")
        for package, version, result in outcomes:
            print("[[package]]")
            print(f"selector = {_toml_value(package.selector)}")
            print(f"path = {_toml_value(str(package.root))}")
            print(f"manifest_version = {_toml_value(version)}")
            print(f"health = {_toml_value('healthy' if result.ok else 'unhealthy')}")
            print(f"update_status = {_toml_value(result.status or 'not-configured')}")
    else:
        state = "complete" if inventory.complete else "incomplete"
        log_info(f"Collection: {inventory.root} ({state})")
        for package, version, result in outcomes:
            status = result.status or ("healthy" if result.ok else "unhealthy")
            log_info(f"{package.selector} [{version}]: {status}")
            for error in result.errors:
                log_error(f"  - {error}")
        for diagnostic in inventory.diagnostics:
            log_error(f"  - {diagnostic}")
        log_info(f"{len(inventory.packages)} packages discovered.")
    return highest


def _collection_list(inventory, *, filter_name: str, toml: bool) -> int:
    """Print the deterministic collection inventory without mutating packages."""
    if toml:
        print("[collection]")
        print(f"root = {_toml_value(str(inventory.root))}")
        print(f"complete = {'true' if inventory.complete else 'false'}")
        for package in inventory.packages:
            print("[[package]]")
            print(f"selector = {_toml_value(package.selector)}")
            print(f"path = {_toml_value(str(package.root))}")
    else:
        log_info(f"Collection: {inventory.root}")
        for package in inventory.packages:
            log_info(package.selector)
        log_info(f"{len(inventory.packages)} packages discovered.")
    # The filter is accepted as part of the stable CLI. Status-aware filtering
    # is performed by callers that request the corresponding aggregate check.
    _ = filter_name
    return EXIT_USER_ERROR if not inventory.complete else EXIT_SUCCESS


def _run_package_tui(package_path: Path) -> int:
    """Open the existing package operation interface for one selected root."""
    try:
        # Bare and selected-package invocations bypass the explicit ``tui``
        # command, so they provision the same optional runtime here.
        from gupkg.dependencies import ensure_runtime_dependencies

        ensure_runtime_dependencies("tui")
        from gupkg.tui import run_tui

        return run_tui(str(package_path))
    except RuntimeError as install_error:
        log_error(f"Could not install a TUI dependency: {install_error}")
        return EXIT_MUTATION_ERROR


def _run_manager_tui(manager, inventory) -> int:
    """Open the fixed-location manager interface for a loaded inventory."""
    try:
        from gupkg.dependencies import ensure_runtime_dependencies

        ensure_runtime_dependencies("tui")
        from gupkg.manager_tui import run_manager_tui

        return run_manager_tui(manager, inventory)
    except RuntimeError as install_error:
        log_error(f"Could not install a TUI dependency: {install_error}")
        return EXIT_MUTATION_ERROR


def _manager_scope(value: str, *, allow_all: bool = True) -> set[Scope]:
    """Convert manager's lowercase scope spelling into configured scopes."""
    choices = {"user": {Scope.USER}, "system": {Scope.MACHINE}}
    if allow_all:
        choices["all"] = {Scope.USER, Scope.MACHINE}
    try:
        return choices[value]
    except KeyError as exc:
        allowed = "user, system, all" if allow_all else "user, system"
        raise ValueError(f"--scope must be one of: {allowed}") from exc


def _manager_targets(inventory, scopes: set[Scope]):
    """Return targets in stable inventory order for the selected scopes."""
    return [target for target in inventory.targets if target.scope in scopes]


def _manager_update(target) -> ActionResult:
    """Refresh one target's update state while keeping CLI output suppressed."""
    manifest = next(
        (item for item in target.package.manifests if item.version_path.name == target.local_version),
        None,
    )
    if manifest is None:
        target.update_status = "error"
        target.diagnostics.append("No manifest is available for the local version")
        return ActionResult(False, errors=target.diagnostics[-1:], exit_code=EXIT_USER_ERROR)
    # Update checks are intentionally the only manager read command allowed to
    # contact an update provider; their ordinary progress belongs outside TOML.
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = check_package_update(manifest.version_path)
    except (ConfigValidationError, ValueError, OSError) as exc:
        target.update_status = "error"
        target.diagnostics.append(str(exc))
        return ActionResult(False, errors=[str(exc)], exit_code=EXIT_USER_ERROR)
    except Exception as exc:
        target.update_status = "error"
        target.diagnostics.append(str(exc))
        return ActionResult(False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)
    if result.status in {"available", "current"}:
        target.update_status = result.status
    elif result.ok:
        target.update_status = "not-configured"
    else:
        target.update_status = "error"
    state = _load_update_state(_update_paths(target.package.root)["state"])
    candidate_id = state.get("lastCandidateId")
    for candidate in state.get("candidates", []):
        if candidate.get("candidateId") == candidate_id:
            target.candidate_version = candidate.get("version")
            break
    target.diagnostics.extend(result.errors)
    return result


def _toml_array(values: list[str]) -> str:
    """Render a stable TOML string array for manager diagnostics."""
    return "[" + ", ".join(_toml_value(value) for value in values) + "]"


def _manager_toml(inventory, targets, *, complete: bool, upgraded: int = 0,
                  skipped: int = 0, failed: int = 0) -> None:
    """Render the issue 011 manager document without interleaved log text."""
    installed = sum(target.installation_status == "installed" for target in targets)
    current = sum(target.update_status == "current" for target in targets)
    print("[manager]")
    print("schema_version = 1")
    print(f"config = {_toml_value(str(inventory.config.path))}")
    print(f"complete = {'true' if complete else 'false'}")
    for target in targets:
        print("\n[[target]]")
        print(f"id = {_toml_value(target.target_id)}")
        print(f"selector = {_toml_value(target.package.selector)}")
        print(f"scope = {_toml_value(scope_id(target.scope))}")
        print(f"path = {_toml_value(str(target.package.root))}")
        print(f"installation = {_toml_value(target.installation_status)}")
        print(f"installed_version = {_toml_value(target.installed_version or '')}")
        print(f"local_version = {_toml_value(target.local_version or '')}")
        print(f"health = {_toml_value(target.health_status)}")
        print(f"update = {_toml_value(target.update_status)}")
        print(f"candidate_version = {_toml_value(target.candidate_version or '')}")
        print("changed = false")
        print(f"errors = {_toml_array(target.diagnostics)}")
        print("warnings = []")
    print("\n[summary]")
    print(f"total = {len(targets)}")
    print(f"installed = {installed}")
    print(f"upgraded = {upgraded}")
    print(f"current = {current}")
    print(f"skipped = {skipped}")
    print(f"failed = {failed}")


def _manager_list(inventory, *, scopes: set[Scope], filter_name: str, toml: bool) -> int:
    """List manager targets using local state, refreshing only updatable views."""
    targets = _manager_targets(inventory, scopes)
    highest = EXIT_SUCCESS
    if filter_name == "updatable":
        for target in targets:
            highest = max(highest, _manager_update(target).exit_code)
        # Failed checks remain visible so an updatable view does not hide the
        # reason a candidate could not be established.
        targets = [target for target in targets if target.update_status in {"available", "error"}]
    elif filter_name == "installed":
        targets = [target for target in targets if target.installation_status == "installed"]
    elif filter_name == "uninstalled":
        targets = [target for target in targets if target.installation_status == "not-installed"]
    elif filter_name == "unhealthy":
        targets = [target for target in targets if target.health_status != "healthy"]
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    if not complete:
        highest = max(highest, EXIT_USER_ERROR)
    if toml:
        _manager_toml(inventory, targets, complete=complete, failed=sum(t.update_status == "error" for t in targets))
    else:
        log_info(f"Manager: {inventory.config.path}")
        for target in targets:
            version = target.installed_version or "-"
            log_info(f"{target.target_id}  {scope_name(target.scope)}  {target.installation_status} {version}  {target.health_status}  {target.update_status}")
        log_info(f"{len(targets)} targets selected.")
    return highest


def _manager_doctor(inventory, *, scopes: set[Scope], toml: bool) -> int:
    """Validate manager roots and every selected manifest without network access."""
    targets = _manager_targets(inventory, scopes)
    highest = EXIT_SUCCESS
    for target in targets:
        if target.health_status != "healthy":
            highest = max(highest, EXIT_USER_ERROR)
        for manifest in target.package.manifests:
            with contextlib.redirect_stdout(io.StringIO()):
                result = health_check_package(manifest.version_path, scope=target.scope)
            if not result.ok:
                target.diagnostics.extend(result.errors)
                target.health_status = "unhealthy"
                highest = max(highest, result.exit_code)
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    if not complete:
        highest = max(highest, EXIT_USER_ERROR)
    if toml:
        _manager_toml(inventory, targets, complete=complete, failed=sum(t.health_status != "healthy" for t in targets))
    else:
        log_info(f"Manager doctor: {inventory.config.path}")
        for target in targets:
            log_info(f"{target.target_id}: {target.health_status}")
            for diagnostic in target.diagnostics:
                log_error(f"  - {diagnostic}")
        for scope in inventory.scopes:
            if scope.scope in scopes:
                for diagnostic in scope.diagnostics:
                    log_error(f"  - {diagnostic}")
    return highest


def _manager_check(inventory, *, scopes: set[Scope], toml: bool) -> int:
    """Refresh update status for all selected targets and aggregate severity."""
    targets = _manager_targets(inventory, scopes)
    highest = EXIT_SUCCESS
    for target in targets:
        highest = max(highest, _manager_update(target).exit_code)
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    if not complete:
        highest = max(highest, EXIT_USER_ERROR)
    if toml:
        _manager_toml(inventory, targets, complete=complete, failed=sum(t.update_status == "error" for t in targets))
    else:
        log_info(f"Manager update check: {inventory.config.path}")
        for target in targets:
            log_info(f"{target.target_id}: {target.update_status}")
    return highest


def _manager_config_path(raw: list[str], explicit: Path | None) -> tuple[Path | None, bool]:
    """Resolve exactly ``--config`` or the current directory marker."""
    if explicit is not None:
        return explicit, True
    marker = Path.cwd() / "gupkg-config.toml"
    return (marker, False) if marker.exists() else (None, False)


def _has_explicit_package_path(arguments: list[str]) -> bool:
    """Recognize package command forms that carry a positional path."""
    if len(arguments) >= 2 and arguments[0] == "install":
        return not arguments[1].startswith("-")
    return len(arguments) >= 3 and arguments[0] in {"upgrade", "config"} and not arguments[2].startswith("-")


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch package commands and collection-wide read-only commands.

    Parameters
    ----------
    argv : list[str] | None, default=None
        Optional command arguments excluding the program name.

    Returns
    -------
    int
        Process exit status shared by console-script and module invocation.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    globals_parser = argparse.ArgumentParser(add_help=False)
    globals_parser.add_argument("--root", type=Path)
    globals_parser.add_argument("--package")
    globals_parser.add_argument("--config", type=Path)
    globals_parser.add_argument("--max-depth", type=int, default=8)
    globals_parser.add_argument("--toml", action="store_true")
    globals_args, remaining = globals_parser.parse_known_args(raw)
    root = (globals_args.root or Path.cwd()).expanduser()
    package_args = (["--toml"] if globals_args.toml else []) + remaining

    # An explicit path retains package-local semantics even from a manager
    # directory; combining it with an unrelated manager config is unsafe.
    explicit_path = _has_explicit_package_path(remaining)
    config_path, config_was_explicit = _manager_config_path(raw, globals_args.config)
    if config_path is not None and explicit_path and config_was_explicit and not globals_args.package:
        log_error("--config cannot be combined with an unrelated explicit package path")
        return EXIT_USER_ERROR

    # The manager marker is deliberately fixed to the caller's directory. A
    # malformed marker is a visible configuration error, never a mode fallback.
    manager_mode = config_path is not None and not (explicit_path and not config_was_explicit)
    if manager_mode:
        try:
            manager = load_manager_config(config_path)
            manager_inventory = discover_manager(manager)
        except (ConfigValidationError, ValueError, OSError) as exc:
            log_error(str(exc))
            return EXIT_USER_ERROR
        if globals_args.package:
            selector_args = list(remaining)
            if selector_args and selector_args[0] in {"list", "doctor"}:
                log_error("--package cannot be combined with manager aggregate commands")
                return EXIT_USER_ERROR
            try:
                target_scope = None
                if "--scope" in selector_args:
                    index = selector_args.index("--scope")
                    target_scope = next(iter(_manager_scope(selector_args[index + 1], allow_all=False)))
                    del selector_args[index:index + 2]
                target = select_target(manager_inventory, globals_args.package, target_scope)
            except (ValueError, IndexError) as exc:
                log_error(str(exc))
                return EXIT_USER_ERROR
            if not selector_args:
                return _run_package_tui(target.package.root)
            selected_package_args = (["--toml"] if globals_args.toml else []) + selector_args
            return _package_main(["--scope", target.scope.value, *selected_package_args, str(target.package.root)])

        command_args = list(remaining)
        manager_parser = argparse.ArgumentParser(prog="gupkg manager")
        manager_parser.add_argument("--scope", choices=["user", "system", "all"], default="all")
        manager_parser.add_argument("--toml", action="store_true")
        manager_parser.add_argument("--filter", choices=["all", "installed", "uninstalled", "updatable", "unhealthy"], default="all")
        manager_parser.add_argument("command", nargs="*", help="list, upgrade check, or doctor")
        try:
            manager_args = manager_parser.parse_args(command_args)
            scopes = _manager_scope(manager_args.scope)
        except SystemExit:
            raise
        except ValueError as exc:
            log_error(str(exc))
            return EXIT_USER_ERROR
        if manager_args.command == []:
            return _run_manager_tui(manager, manager_inventory)
        if manager_args.command == ["list"]:
            return _manager_list(manager_inventory, scopes=scopes, filter_name=manager_args.filter, toml=globals_args.toml or manager_args.toml)
        if manager_args.command == ["upgrade", "check"]:
            return _manager_check(manager_inventory, scopes=scopes, toml=globals_args.toml or manager_args.toml)
        if manager_args.command == ["doctor"]:
            return _manager_doctor(manager_inventory, scopes=scopes, toml=globals_args.toml or manager_args.toml)
        log_error("Manager mode supports 'list', 'upgrade check', and 'doctor'.")
        return EXIT_USER_ERROR

    # Explicit package paths bypass implicit manager discovery and preserve the
    # existing package parser, including its Auto/User/Machine scope syntax.
    if explicit_path:
        return _package_main(package_args)

    from gupkg.collection import discover_collection, select_package

    # Explicit selection always resolves against an inventory, never against
    # manifest metadata or an arbitrary recursive filename search.
    try:
        inventory = discover_collection(root, max_depth=globals_args.max_depth)
    except ValueError as exc:
        log_error(str(exc))
        return EXIT_USER_ERROR
    try:
        resolve_input_path(root)
        package_context = True
    except ValueError:
        package_context = False
    if globals_args.package:
        try:
            selected = select_package(inventory, globals_args.package)
        except ValueError as exc:
            log_error(str(exc))
            return EXIT_USER_ERROR
        if not remaining:
            # The package TUI already supports an editable path. Passing the
            # selected root to its existing command contract retains one UI.
            return _run_package_tui(selected.root)
        if remaining[0] == "list":
            log_error("list is collection-only and cannot be combined with --package")
            return EXIT_USER_ERROR
        return _package_main([*package_args, str(selected.root)])

    # An explicit path is already an unambiguous package selection. Route it
    # directly instead of treating the caller's working directory as a
    # collection whose mutations would require ``--package``.
    explicit_package_path = (
        len(remaining) >= 2
        and remaining[0] == "install"
        or len(remaining) >= 3
        and remaining[0] in {"upgrade", "config"}
    )
    if explicit_package_path:
        return _package_main(package_args)

    # A real package root takes precedence over aggregate command spellings.
    # This preserves ``gupkg config check`` inside one package.
    if package_context:
        if not remaining:
            return _run_package_tui(root)
        return _package_main(package_args)

    if remaining and remaining[0] == "list":
        list_parser = argparse.ArgumentParser(prog="gupkg list")
        list_parser.add_argument("--filter", choices=["all", "updatable", "unhealthy"], default="all")
        list_args = list_parser.parse_args(remaining[1:])
        return _collection_list(inventory, filter_name=list_args.filter, toml=globals_args.toml)
    if len(remaining) >= 2 and remaining[0:2] == ["config", "check"]:
        return _aggregate_check(inventory, update=False, scope=Scope.AUTO, toml=globals_args.toml)
    if len(remaining) >= 2 and remaining[0:2] == ["upgrade", "check"]:
        return _aggregate_check(inventory, update=True, scope=Scope.AUTO, toml=globals_args.toml)

    # A resolvable root retains ordinary package behavior. Every other bare
    # invocation is a collection context, where no implicit mutation is safe.
    if not remaining:
        if globals_args.toml:
            return _collection_list(inventory, filter_name="all", toml=True)
        try:
            # Collection mode is also a Textual interface, even though it
            # presents a selector before opening a package's operation screen.
            from gupkg.dependencies import ensure_runtime_dependencies

            ensure_runtime_dependencies("tui")
            from gupkg.collection_tui import run_collection_tui

            return run_collection_tui(inventory)
        except RuntimeError as install_error:
            log_error(f"Could not install a TUI dependency: {install_error}")
            return EXIT_MUTATION_ERROR
    if remaining[0] in {"install", "upgrade", "config"}:
        log_error("Collection mutations require --package or an explicit package path.")
        return EXIT_USER_ERROR
    return _package_main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
