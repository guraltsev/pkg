#!/usr/bin/env python3
"""Install and maintain local Windows packages declared by ``pkg.toml``.

The module is the stable executable and Python facade for package actions. It
coordinates configuration, origin population, component installation, and
updates while focused implementation domains live in the ``gupkg`` package.

Usage and API
-------------
Call the package workflow functions directly. Embedders may call
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

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

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
    __version__ as _RUNTIME_VERSION,
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
    populate_app_from_origin,
    validate_origin_health,
    validate_update_health,
)
from gupkg.updates import (  # noqa: E402
    check_update,
    git_origin_candidate,
    load_update_state,
    _next_version_identity,
    prepare_update,
    _toml_value,
    update_paths,
    _write_update_state,
)
from gupkg.windows import is_current_user_admin  # noqa: E402

__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4


# Re-export the shared runtime identity for callers that inspect this facade.
__version__ = _RUNTIME_VERSION




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
    paths = update_paths(identity.package_root)
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
        state = load_update_state(paths["state"])
        state["lastAttemptedCheck"] = datetime.now(timezone.utc).isoformat()
        if bootstrap and config["update"]["check"]["mode"] == "git":
            status = "available"
            candidate = git_origin_candidate(identity, config, state)
        else:
            status, candidate = check_update(
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
    paths = update_paths(identity.package_root)
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
        state = load_update_state(paths["state"])
        if bootstrap and update["check"]["mode"] == "git":
            status = "available"
            candidate = git_origin_candidate(identity, config, state)
        else:
            status, candidate = check_update(
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
            # immutable promotion rather than staging another version.
            if identity.version.startswith("bootstrap"):
                paths["receipts"].mkdir(parents=True, exist_ok=True)
                write_text_atomic(
                    receipt,
                    f"schemaVersion = 1\ncandidateId = {_toml_value(candidate['candidateId'])}\nversion = {_toml_value(new_identity.version)}\nlocalVersion = {new_identity.local_version}\n",
                )
            else:
                return action_failure(
                    "Cannot stage update because its immutable version already "
                    f"exists: {new_identity.version_path}",
                    exit_code=EXIT_USER_ERROR,
                    warnings=warnings,
                )
            log_info(f"Downloaded: {new_identity.version_string}")
            return ActionResult(True, warnings=warnings, status="downloaded")
        staged = prepare_update(
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
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    no_checksum: bool = False,
    shim_linkage: str = "dynamic",
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
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used while installing the activated update.

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
    receipts = update_paths(identity.package_root)["receipts"]
    receipt_paths = sorted(
        receipts.glob("v*.toml"), key=lambda path: path.stat().st_mtime, reverse=True
    ) if receipts.exists() else []
    if not receipt_paths:
        return action_failure(
            "No downloaded update is available. Run 'gupkg update --download-only' first.",
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
        version_name = f"v{version}"
        if local_version:
            version_name += f".l{local_version}"
        version_path = identity.package_root / version_name
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
            "'gupkg update --download-only' first.",
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
    result = install_package(version_path, scope=scope, shim_linkage=shim_linkage)
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
    shim_linkage: str = "dynamic",
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
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used while activating the downloaded update.

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
    install_result = install_downloaded_update(
        package_path, scope=scope, shim_linkage=shim_linkage
    )
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
    install_context=None,
    shim_linkage: str = "dynamic",
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
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used for generated executable wrappers.

    Returns
    -------
    ActionResult
        Truthful description of the install outcome and recommended exit code.

    """
    print_action_banner("install", scope)

    # Reject an invalid launcher preference before package activation can make
    # any filesystem changes.
    if shim_linkage not in {"dynamic", "static"}:
        return action_failure(
            "shim linkage must be either 'dynamic' or 'static'",
            exit_code=EXIT_USER_ERROR,
        )

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
    # package definitions; authors must run `gupkg config-fix` explicitly.
    inconsistencies = check_metadata_consistency(identity, raw_config_data)
    if inconsistencies:
        log_error("Configuration inconsistencies detected:")
        for message in inconsistencies:
            log_error(f"  - {message}")
        log_info("Run this command before installing:")
        log_info(f"  gupkg config-fix {identity.version_path}")
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
        scope_paths = (
            install_context.as_scope_paths()
            if install_context is not None
            else compute_scope_paths(scope)
        )
        scope_paths["shim_linkage"] = shim_linkage
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
        result = install_downloaded_update(
            identity.version_path, scope=scope, shim_linkage=shim_linkage
        )
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

    # App population is an explicit origin operation; packages without an
    # origin proceed directly to their declared component work.
    origin_changed = False
    if runtime_config.get("origin") is not None:
        log_info("")
        origin_result = populate_app_from_origin(
            identity,
            runtime_config,
            no_checksum=no_checksum,
            refresh_app=refresh_app,
        )
        warnings.extend(origin_result.warnings)
        origin_changed = origin_result.changed
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
        changed=junction_changed or origin_changed or component_result.changed,
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
    print_action_banner("config-fix", scope)

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
    print_action_banner("config-check", scope)

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
