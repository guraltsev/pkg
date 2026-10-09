"""Install and maintain local Windows packages declared by ``pkg.toml``.

The module is the Python facade for single-package actions. It coordinates
configuration, origin population, component installation, and the
check → download → install update lifecycle while focused implementation
domains live in the other ``gupkg`` modules. Every workflow returns an
:class:`~gupkg.core.ActionResult`; progress is logged to stdout, and errors are
returned rather than printed so the caller decides how to present them.

Usage and API
-------------
Call ``install_package(...)``, ``health_check_package(...)``,
``check_package_update(...)``, ``download_package_update(...)``,
``install_downloaded_update(...)``, or ``full_package_upgrade(...)``. The
command line in :mod:`gupkg.cli` and the manager both delegate to these
functions.

Implementation Approach
-----------------------
Each action resolves one package version from the directory layout and loads
its validated configuration before mutating anything. Update checks and
downloads run inside one package-root lock with a disposable work directory;
a download commits a complete staged version with a single rename and leaves a
receipt that a later activation consumes through the ordinary install path.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from gupkg._version import __version__
from gupkg.components import install_components
from gupkg.configuration import check_metadata_consistency, read_runtime_config
from gupkg.core import (
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    compare_package_versions,
    is_version_directory_name,
    log_info,
    log_warning,
    read_toml_file,
)
from gupkg.layout import compute_scope_paths, resolve_input_path, update_current_junction_if_needed
from gupkg.origin import populate_app_from_origin, validate_origin_health, validate_update_health
from gupkg.updates import (
    check_update,
    git_origin_candidate,
    load_update_state,
    next_version_identity,
    prepare_update,
    update_paths,
    write_receipt,
    write_update_state,
)
from gupkg.windows import is_current_user_admin

__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"
__all__ = [
    "EXIT_INTERNAL_ERROR",
    "EXIT_MUTATION_ERROR",
    "EXIT_SUCCESS",
    "EXIT_USER_ERROR",
    "__version__",
    "check_package_update",
    "download_package_update",
    "full_package_upgrade",
    "health_check_package",
    "install_downloaded_update",
    "install_package",
]


# ---------------------------------------------------------------------------
# Install and health check
# ---------------------------------------------------------------------------


def install_package(
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    allow_downgrade: bool = False,
    refresh_app: bool = False,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
    install_context=None,
    shim_linkage: str = "dynamic",
) -> ActionResult:
    """Install or reinstall a package and return a truthful action result.

    Same-version installs are intentionally not a no-op: the fixed component
    sequence reruns so broken shortcuts, environment variables, PATH entries,
    and wrapper files are restored, and ``current`` may be refreshed.

    Parameters
    ----------
    package_path : Path
        Version directory, package root, or ``current`` junction.
    scope : Scope, default=Scope.AUTO
        Installation scope. Automatic selection uses machine scope for
        administrators unless the package is portable-only.
    allow_downgrade : bool, default=False
        Whether ``current`` may be replaced when it points to a newer version.
    refresh_app : bool, default=False
        Whether to repopulate ``App/`` from origin even when it has files.
    no_checksum : bool, default=False
        Whether to skip configured origin checksum verification.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies
        while promoting a bootstrap package.
    install_context : InstallationContext, optional
        Manager-owned destinations that replace the scope's default shortcut
        and wrapper locations.
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used for generated executable wrappers.

    Returns
    -------
    ActionResult
        Install outcome and recommended exit code.

    """
    _print_action_banner("install", scope)
    if shim_linkage not in {"dynamic", "static"}:
        return _failure("shim linkage must be either 'dynamic' or 'static'", EXIT_USER_ERROR)

    # Resolve the version and load its configuration before any mutation, so
    # layout, validation, and metadata problems stop the install early.
    try:
        identity, installing_from_current = resolve_input_path(Path(package_path))
        runtime_config, raw_config, warnings = read_runtime_config(identity)
    except (ConfigValidationError, ValueError) as exc:
        return _failure(f"Failed to load package metadata/config: {exc}", EXIT_USER_ERROR)
    for warning in warnings:
        log_warning(warning)
    inconsistencies = check_metadata_consistency(identity, raw_config)
    if inconsistencies:
        log_info(
            "Configuration inconsistencies detected; run "
            f"'gupkg config-fix {identity.version_path}' before installing."
        )
        return _failure(inconsistencies, EXIT_USER_ERROR, warnings)
    log_info(f"Package: {identity.name}")
    log_info(f"Version: {identity.version_string}")
    log_info(f"Path: {identity.version_path}")
    log_info(f"only_portable: {runtime_config['only_portable']}")
    log_info("")

    # Resolve automatic scope from the portability policy, then reject scope
    # combinations the package model cannot support.
    is_admin = is_current_user_admin() if scope in {Scope.AUTO, Scope.MACHINE} else False
    if scope == Scope.AUTO:
        scope = Scope.MACHINE if is_admin and not runtime_config["only_portable"] else Scope.USER
        log_info(f"Selected scope: {scope.value}")
        log_info("")
    if runtime_config["only_portable"] and scope == Scope.MACHINE:
        return _failure(
            "only_portable packages cannot be installed system-wide. Please use User scope.",
            EXIT_USER_ERROR,
            warnings,
        )
    if scope == Scope.MACHINE and not is_admin:
        return _failure(
            "Machine scope requires administrator privileges. Please run as administrator.",
            EXIT_USER_ERROR,
            warnings,
        )
    try:
        scope_paths: Dict[str, Any] = (
            install_context.as_scope_paths()
            if install_context is not None
            else compute_scope_paths(scope)
        )
    except (RuntimeError, ValueError, OSError) as exc:
        return _failure(f"Failed to resolve {scope.value} scope paths: {exc}", EXIT_MUTATION_ERROR, warnings)
    scope_paths["shim_linkage"] = shim_linkage

    # Bootstrap versions are templates, never installed versions: stage the
    # first immutable version and install that instead.
    if _is_update_bootstrap(identity, runtime_config):
        log_info("Promoting bootstrap into an immutable package version...")
        downloaded = download_package_update(
            identity.version_path,
            no_checksum=no_checksum,
            local_deps_autoinstall=local_deps_autoinstall,
        )
        if not downloaded.ok:
            downloaded.warnings = warnings + downloaded.warnings
            return downloaded
        result = install_downloaded_update(
            identity.version_path,
            scope=scope,
            shim_linkage=shim_linkage,
            install_context=install_context,
        )
        result.warnings = warnings + downloaded.warnings + result.warnings
        return result

    # Point ``current`` at this version unless the caller already targeted
    # ``current``. A newer active version wins unless a downgrade is allowed.
    junction_changed = False
    if installing_from_current:
        log_info("Installing from resolved 'current' target (skipping junction management)")
    else:
        log_info("Managing 'current' junction...")
        try:
            junction_changed = update_current_junction_if_needed(
                identity, allow_downgrade=allow_downgrade
            )
        except ValueError as exc:
            return _failure(str(exc), EXIT_USER_ERROR, warnings)
        except Exception as exc:
            return _failure(str(exc), EXIT_MUTATION_ERROR, warnings)
        if not junction_changed and not identity.is_current:
            log_info("Skipping component installation (newer version already installed)")
            return ActionResult(ok=True, warnings=warnings)

    # Populate App when it is missing or a refresh was requested; packages
    # without an origin proceed directly to their component work.
    origin_result = populate_app_from_origin(
        identity, runtime_config, no_checksum=no_checksum, refresh_app=refresh_app
    )
    warnings.extend(origin_result.warnings)
    if not origin_result.ok:
        return ActionResult(
            ok=False,
            changed=junction_changed or origin_result.changed,
            warnings=warnings,
            errors=[f"Origin population failed: {error}" for error in origin_result.errors],
            exit_code=EXIT_MUTATION_ERROR,
        )

    # Apply the fixed component sequence only after App is ready.
    log_info("")
    log_info("Installing components...")
    component_result = install_components(identity, scope, scope_paths, runtime_config)
    warnings.extend(component_result.warnings)
    changed = junction_changed or origin_result.changed or component_result.changed
    if not component_result.ok:
        return ActionResult(
            ok=False,
            changed=changed,
            warnings=warnings,
            errors=component_result.errors,
            exit_code=EXIT_MUTATION_ERROR,
        )
    return ActionResult(ok=True, changed=changed, warnings=warnings)


def health_check_package(package_path: Path, *, scope: Scope = Scope.USER) -> ActionResult:
    """Validate one package configuration without mutating state.

    Parameters
    ----------
    package_path : Path
        Version directory, package root, or ``current`` junction.
    scope : Scope, default=Scope.USER
        Selected CLI scope, used only for the banner.

    Returns
    -------
    ActionResult
        Validation outcome; ``errors`` lists every metadata, origin, and
        update-hook problem found.

    """
    _print_action_banner("config-check", scope)
    try:
        identity, _ = resolve_input_path(Path(package_path))
        runtime_config, raw_config, warnings = read_runtime_config(identity)
    except (ConfigValidationError, ValueError) as exc:
        return _failure(f"Failed to load package metadata/config: {exc}", EXIT_USER_ERROR)
    for warning in warnings:
        log_warning(warning)

    # Report every problem at once instead of stopping at the first.
    errors = check_metadata_consistency(identity, raw_config)
    errors.extend(validate_origin_health(identity, runtime_config.get("origin")))
    errors.extend(validate_update_health(identity, runtime_config.get("update")))
    if errors:
        return _failure(errors, EXIT_USER_ERROR, warnings)
    log_info(f"Package: {identity.name}")
    log_info(f"Version: {identity.version_string}")
    log_info(f"Path: {identity.version_path}")
    log_info("Health check passed.")
    return ActionResult(ok=True, warnings=warnings)


# ---------------------------------------------------------------------------
# Updates: check → download → install
# ---------------------------------------------------------------------------


def check_package_update(
    package_path: Path, *, local_deps_autoinstall: bool = False
) -> ActionResult:
    """Check one package's configured source and persist the result.

    Parameters
    ----------
    package_path : Path
        Any version directory, package root, or ``current`` junction; a
        historical version may provide the update configuration.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.

    Returns
    -------
    ActionResult
        ``status`` is ``available``, ``current``, or ``not-configured``.
    """
    return _run_update(
        package_path, stage=False, no_checksum=False, local_deps_autoinstall=local_deps_autoinstall
    )


def download_package_update(
    package_path: Path,
    *,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
) -> ActionResult:
    """Check, download, and stage an available package update without activating it.

    Parameters
    ----------
    package_path : Path
        Version directory, package root, or ``current`` junction.
    no_checksum : bool, default=False
        Whether checksum verification may be bypassed for downloaded payloads.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.

    Returns
    -------
    ActionResult
        ``status`` is ``downloaded`` when a staged version is waiting for
        activation (including one staged by an earlier download), ``current``
        when no update is available, or ``not-configured``.
    """
    return _run_update(
        package_path,
        stage=True,
        no_checksum=no_checksum,
        local_deps_autoinstall=local_deps_autoinstall,
    )


def install_downloaded_update(
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    shim_linkage: str = "dynamic",
    install_context=None,
) -> ActionResult:
    """Activate the most recently downloaded update for a package.

    Parameters
    ----------
    package_path : Path
        Version directory, package root, or ``current`` junction that owns
        staged updates.
    scope : Scope, default=Scope.AUTO
        Installation scope used to activate the staged version.
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used while installing the activated update.
    install_context : InstallationContext, optional
        Manager-owned destinations forwarded to :func:`install_package`.

    Returns
    -------
    ActionResult
        Activation outcome; ``status`` is ``installed-update`` on success.
    """
    try:
        identity, _ = resolve_input_path(Path(package_path))
        receipt_path, version_path = _newest_receipt(identity)
    except (OSError, ValueError) as exc:
        return _failure(str(exc), EXIT_USER_ERROR)

    # A receipt for this version or an older one was already consumed or
    # superseded, so it must not turn an upgrade into a reinstall.
    no_upgrade = (
        "No downloaded upgrade is waiting to be installed. "
        "Run 'gupkg update --download-only' first."
    )
    if (
        not identity.version.startswith("bootstrap")
        and compare_package_versions(version_path.name, identity.version_string) <= 0
    ):
        return _failure(no_upgrade, EXIT_USER_ERROR)

    # Never let an older staged definition replace a newer installed version.
    newer = sorted(
        (
            path.name
            for path in identity.package_root.iterdir()
            if path.is_dir()
            and is_version_directory_name(path.name)
            and not path.name.startswith("vbootstrap")
            and compare_package_versions(path.name, version_path.name) > 0
        ),
        key=lambda name: name.casefold(),
    )
    if newer:
        return _failure(
            f"Cannot activate downloaded update because a newer installed version exists: {newer[-1]}",
            EXIT_USER_ERROR,
        )
    result = install_package(
        version_path, scope=scope, shim_linkage=shim_linkage, install_context=install_context
    )
    if result.ok:
        receipt_path.unlink(missing_ok=True)
        result.status = "installed-update"
    return result


def full_package_upgrade(
    package_path: Path,
    *,
    scope: Scope = Scope.AUTO,
    no_checksum: bool = False,
    local_deps_autoinstall: bool = False,
    shim_linkage: str = "dynamic",
    install_context=None,
) -> ActionResult:
    """Check, stage, and activate an available update in one operation.

    Parameters
    ----------
    package_path : Path
        Version directory, package root, or ``current`` junction.
    scope : Scope, default=Scope.AUTO
        Installation scope used when activating the staged version.
    no_checksum : bool, default=False
        Whether checksum verification may be bypassed while staging a payload.
    local_deps_autoinstall : bool, default=False
        Whether package-local update hooks may install missing dependencies.
    shim_linkage : {"dynamic", "static"}, default="dynamic"
        Native launcher linkage used while activating the downloaded update.
    install_context : InstallationContext, optional
        Manager-owned destinations forwarded to the activation.

    Returns
    -------
    ActionResult
        The download result when nothing was staged, otherwise the activation
        result with the download warnings prepended.
    """
    downloaded = download_package_update(
        package_path, no_checksum=no_checksum, local_deps_autoinstall=local_deps_autoinstall
    )
    if not downloaded.ok or downloaded.status != "downloaded":
        return downloaded
    installed = install_downloaded_update(
        package_path, scope=scope, shim_linkage=shim_linkage, install_context=install_context
    )
    installed.warnings = downloaded.warnings + installed.warnings
    return installed


def _run_update(
    package_path: Path, *, stage: bool, no_checksum: bool, local_deps_autoinstall: bool
) -> ActionResult:
    """Run one locked update check and optionally stage the discovered candidate."""
    warnings: List[str] = []
    try:
        # Any package version may define the update source; persistent update
        # state remains package-root owned either way.
        identity, _ = resolve_input_path(Path(package_path))
        config, _, warnings = read_runtime_config(identity)
        if config.get("update") is None:
            return ActionResult(
                True,
                warnings=warnings + ["Updates are not configured for this package"],
                status="not-configured",
            )
        with _update_session(identity.package_root) as (paths, work):
            status, candidate = _discover_update(
                identity, config, paths, local_deps_autoinstall=local_deps_autoinstall
            )
            if candidate is None:
                log_info(f"Current: {identity.version_string}")
                return ActionResult(True, warnings=warnings, status=status)
            log_info(f"Available: v{candidate['version']} ({candidate['candidateId']})")
            if not stage:
                return ActionResult(True, warnings=warnings, status=status)
            changed = _stage_candidate(
                identity,
                config,
                candidate,
                paths,
                work,
                no_checksum=no_checksum,
                local_deps_autoinstall=local_deps_autoinstall,
            )
            return ActionResult(True, changed=changed, warnings=warnings, status="downloaded")
    except (ConfigValidationError, ValueError) as exc:
        return _failure(str(exc), EXIT_USER_ERROR, warnings)
    except Exception as exc:
        return _failure(str(exc), EXIT_MUTATION_ERROR, warnings)


@contextlib.contextmanager
def _update_session(package_root: Path) -> Iterator[Tuple[Dict[str, Path], Path]]:
    """Hold the package-root update lock and a disposable work directory.

    Concurrent checks and downloads of one package would overwrite each
    other's state and staging, so a second session fails immediately. Both the
    lock and the work directory are removed however the session ends.
    """
    paths = update_paths(package_root)
    paths["locks"].mkdir(parents=True, exist_ok=True)
    lock = paths["locks"] / "update.toml"
    try:
        with open(lock, "x", encoding="utf-8") as handle:
            handle.write(f"pid = {os.getpid()}\n")
    except FileExistsError:
        raise RuntimeError(f"An update operation is already active: {lock}") from None
    work = paths["work"] / str(uuid.uuid4())
    try:
        work.mkdir(parents=True)
        yield paths, work
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.unlink(missing_ok=True)


def _discover_update(
    identity: PackageIdentity,
    config: Dict[str, Any],
    paths: Dict[str, Path],
    *,
    local_deps_autoinstall: bool,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Contact the configured source and persist timing and candidate state."""
    state = load_update_state(paths["state"])
    state["lastAttemptedCheck"] = datetime.now(timezone.utc).isoformat()
    try:
        # A Git bootstrap has no checkout to compare, so its origin ref is
        # always the candidate to promote.
        if _is_update_bootstrap(identity, config) and config["update"]["check"]["mode"] == "git":
            status, candidate = "available", git_origin_candidate(identity, config, state)
        else:
            status, candidate = check_update(
                identity, config, state, local_deps_autoinstall=local_deps_autoinstall
            )
    except Exception as exc:
        # Record the failure for diagnosis, but never let a state-write
        # problem hide the check error itself.
        state["lastError"] = str(exc)
        with contextlib.suppress(OSError):
            write_update_state(paths["state"], state)
        raise
    state.update({
        "lastSuccessfulCheck": datetime.now(timezone.utc).isoformat(),
        "lastStatus": status,
        "lastError": None,
    })
    if candidate is not None:
        state["lastCandidateId"] = candidate["candidateId"]
        state["lastCandidateVersion"] = candidate["version"]
    write_update_state(paths["state"], state)
    return status, candidate


def _stage_candidate(
    identity: PackageIdentity,
    config: Dict[str, Any],
    candidate: Dict[str, Any],
    paths: Dict[str, Path],
    work: Path,
    *,
    no_checksum: bool,
    local_deps_autoinstall: bool,
) -> bool:
    """Commit the candidate as a new immutable version and record its receipt.

    Returns whether a new version directory was created. A version that an
    earlier download staged from this same candidate (its receipt is still
    pending), or a bootstrap's original promotion, is reused instead of rebuilt.
    A different candidate for an existing version is refused.
    """
    new_identity = next_version_identity(identity, candidate)
    receipt = paths["receipts"] / f"{new_identity.version_string}.toml"
    changed = not new_identity.version_path.exists()
    if not changed and not (
        _receipt_candidate(receipt) == candidate["candidateId"]
        or identity.version.startswith("bootstrap")
    ):
        raise ValueError(
            "Cannot stage update because its immutable version already exists: "
            f"{new_identity.version_path}"
        )
    if changed:
        staged = prepare_update(
            identity,
            config,
            candidate,
            work,
            no_checksum=no_checksum,
            local_deps_autoinstall=local_deps_autoinstall,
        )
        os.replace(staged, new_identity.version_path)
    paths["receipts"].mkdir(parents=True, exist_ok=True)
    write_receipt(receipt, candidate, new_identity)
    log_info(f"Downloaded: {new_identity.version_string}")
    return changed


def _receipt_candidate(receipt: Path) -> Optional[str]:
    """Return the candidate ID recorded by a pending receipt, if it is readable."""
    try:
        candidate_id = read_toml_file(receipt).get("candidateId")
    except Exception:
        return None
    return candidate_id if isinstance(candidate_id, str) else None


def _newest_receipt(identity: PackageIdentity) -> Tuple[Path, Path]:
    """Return the newest download receipt and the version directory it names."""
    receipts = update_paths(identity.package_root)["receipts"]
    receipt_paths = (
        sorted(receipts.glob("v*.toml"), key=lambda path: path.stat().st_mtime, reverse=True)
        if receipts.exists()
        else []
    )
    if not receipt_paths:
        raise ValueError(
            "No downloaded update is available. Run 'gupkg update --download-only' first."
        )
    try:
        receipt = read_toml_file(receipt_paths[0])
    except Exception as exc:
        raise ValueError(f"Cannot activate downloaded update: {exc}") from exc
    version, local_version = receipt.get("version"), receipt.get("localVersion")
    if not isinstance(version, str) or not isinstance(local_version, int):
        raise ValueError("Cannot activate downloaded update: receipt has invalid version metadata")
    version_name = f"v{version}" + (f".l{local_version}" if local_version else "")
    version_path = identity.package_root / version_name
    if not version_path.is_dir():
        raise ValueError(f"Cannot activate downloaded update: downloaded version is missing: {version_path}")
    return receipt_paths[0], version_path


def _is_update_bootstrap(identity: PackageIdentity, config: Dict[str, Any]) -> bool:
    """Return whether a ``bootstrap*`` template should stage its first version.

    A Git bootstrap pairs a Git origin with Git check and payload; a release
    bootstrap pairs a GitHub or module check with a ZIP or module payload.
    """
    update = config.get("update")
    if not identity.version.startswith("bootstrap") or update is None:
        return False
    check, payload = update["check"]["mode"], update["payload"]["mode"]
    origin = config.get("origin")
    git_bootstrap = (
        origin is not None and origin.get("mode") == "git" and check == "git" and payload == "git"
    )
    return git_bootstrap or (check in {"github", "module"} and payload in {"zip", "module"})


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _print_action_banner(operation: str, scope: Scope) -> None:
    """Emit the standard progress banner for one operation."""
    log_info("")
    log_info("=" * 60)
    log_info("gupkg: Package Manager")
    log_info(f"Operation: {operation}")
    log_info(f"Scope: {scope.value}")
    log_info("=" * 60)
    log_info("")


def _failure(
    errors: str | List[str], exit_code: int, warnings: Optional[List[str]] = None
) -> ActionResult:
    """Create a failed action result without printing it."""
    return ActionResult(
        ok=False,
        warnings=list(warnings or []),
        errors=[errors] if isinstance(errors, str) else list(errors),
        exit_code=exit_code,
    )
