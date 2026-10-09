"""Populate application payloads from declared package origins.

Git, zip, script, and module origins populate ``App/``. Git and zip origins
prepare a complete temporary application tree before replacing ``App/``;
script and module origins write ``App/`` themselves and are verified afterwards.
Source refs, checksums, archive paths, and package-local references are
validated before the existing payload is mutated.

Usage and API
-------------
Call ``populate_app_from_origin(...)`` during installation and
``validate_origin_health(...)`` or ``validate_update_health(...)`` for
read-only configuration checks. ``safe_extract_zip(...)``,
``copy_zip_extract_mappings(...)``, and ``copy_directory_contents(...)`` are
shared with update staging.

Implementation Approach
-----------------------
Origin selection is performed by normalized configuration. Clones and
downloads write into temporary staging directories inside the version; the
validated contents are then moved into place with recovery of the previous
application directory on error.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional

from .core import PackageIdentity, StepResult, log_info, log_warning
from .downloads import download_response, file_sha256


def app_has_payload(identity: PackageIdentity) -> bool:
    """Return whether a package version has a non-empty ``App`` directory.

    Parameters
    ----------
    identity : PackageIdentity
        Concrete package version whose payload should be inspected.

    Returns
    -------
    bool
        ``True`` only when ``App`` is a directory containing at least one entry.
    """
    app = identity.version_path / "App"
    return app.is_dir() and any(app.iterdir())


def populate_app_from_origin(
    identity: PackageIdentity,
    runtime_config: Dict[str, Any],
    *,
    no_checksum: bool = False,
    refresh_app: bool = False,
) -> StepResult:
    """Populate ``App`` from the package origin when it is missing, empty, or refreshed.

    Parameters
    ----------
    identity : PackageIdentity
        Package version to populate.
    runtime_config : Dict[str, Any]
        Normalized configuration containing ``origin``.
    no_checksum : bool, default=False
        Skip a configured ZIP checksum with a warning.
    refresh_app : bool, default=False
        Replace an already populated ``App``.

    Returns
    -------
    StepResult
        ``changed`` is true when ``App`` was (re)populated; ``errors`` holds the
        failure reason otherwise.
    """
    origin = runtime_config.get("origin")
    if origin is None:
        return StepResult(ok=True)

    # A populated App is left alone unless the caller explicitly refreshes it.
    populated = app_has_payload(identity)
    if populated and not refresh_app:
        log_info("App is already populated; skipping origin population")
        return StepResult(ok=True)
    if populated:
        log_info("--refresh-app enabled; replacing App from origin")
    elif (identity.version_path / "App").exists():
        log_info("App is empty; populating from origin...")
    else:
        log_info("App is missing; populating from origin...")

    # A historical-version entry may record only a version and no provider.
    mode = origin.get("mode")
    try:
        if mode == "zip":
            populate_app_from_zip_origin(identity, origin, no_checksum=no_checksum)
        elif mode == "git":
            populate_app_from_git_origin(identity, origin)
        elif mode == "script":
            populate_app_from_script_origin(identity, origin, runtime_config, refresh_app=refresh_app)
        elif mode == "module":
            populate_app_from_module_origin(identity, origin, runtime_config, refresh_app=refresh_app)
        else:
            raise RuntimeError(
                f"Origin version '{origin.get('version', 'unknown')}' does not declare "
                "url, script, or module, so it cannot populate App"
            )
    except Exception as exc:
        return StepResult(ok=False, errors=[str(exc)])
    if not app_has_payload(identity):
        return StepResult(
            ok=False,
            changed=True,
            errors=["Origin population completed but App is missing or empty"],
        )
    return StepResult(ok=True, changed=True)


def populate_app_from_git_origin(identity: PackageIdentity, origin: Dict[str, str]) -> None:
    """Populate ``App/`` with the exact commit at a configured Git ref."""
    with tempfile.TemporaryDirectory(prefix=".gupkg-origin-", dir=str(identity.version_path)) as temp:
        prepared_app = Path(temp) / "App.new"

        def git(*arguments: str) -> str:
            """Run one Git command quietly and return its stripped output."""
            return subprocess.run(
                ["git", *arguments], capture_output=True, text=True, check=True
            ).stdout.strip()

        # Resolve the configured ref before cloning so the installed checkout
        # records one exact source state even if the branch advances.
        log_info(f"Cloning Git origin: {origin['url']} ({origin['ref']})")
        remote = git("ls-remote", "--exit-code", origin["url"], origin["ref"]).split()[0]
        git("clone", "--no-checkout", origin["url"], str(prepared_app))
        git("-C", str(prepared_app), "fetch", "--no-tags", "origin", origin["ref"])
        fetched = git("-C", str(prepared_app), "rev-parse", "FETCH_HEAD")
        if fetched != remote:
            raise RuntimeError("Configured Git ref changed during origin population; retry installation")
        git("-C", str(prepared_app), "checkout", "--detach", fetched)
        _replace_app(identity.version_path, prepared_app)


def populate_app_from_zip_origin(
    identity: PackageIdentity, origin: Dict[str, str], *, no_checksum: bool
) -> None:
    """Populate ``App/`` from a downloaded and optionally verified zip archive."""
    with tempfile.TemporaryDirectory(prefix=".gupkg-origin-", dir=str(identity.version_path)) as temp:
        temp_root = Path(temp)
        archive_path = temp_root / "origin.zip"
        staging_dir = temp_root / "extract"
        prepared_app = temp_root / "App.new"
        staging_dir.mkdir()

        log_info(f"Downloading origin: {origin['url']}")
        with urllib.request.urlopen(origin["url"], timeout=60) as response:
            download_response(response, archive_path, label="Downloading origin")

        # Verify before extraction so an unexpected archive is never unpacked.
        checksum = origin.get("checksum")
        if checksum and no_checksum:
            log_warning("Checksum verification skipped because --no-checksum was provided")
        elif checksum:
            log_info("Verifying sha256 checksum...")
            if file_sha256(archive_path) != checksum.split(":", 1)[1].lower():
                raise RuntimeError("[origin].checksum did not match downloaded file")

        # Extract safely and select the configured subdirectory, which must
        # stay inside the archive.
        log_info("Extracting zip archive...")
        safe_extract_zip(archive_path, staging_dir)
        selected_source = staging_dir
        if origin.get("extractSubdir"):
            log_info(f"Using archive subdirectory: {origin['extractSubdir']}")
            selected_source = staging_dir / origin["extractSubdir"]
        if not selected_source.resolve().is_relative_to(staging_dir.resolve()):
            raise RuntimeError("[origin].extractSubdir cannot escape the archive")
        if not selected_source.is_dir():
            raise RuntimeError("[origin].extractSubdir was not found in the archive")

        prepared_app.mkdir()
        copy_directory_contents(selected_source, prepared_app)
        _replace_app(identity.version_path, prepared_app)


def populate_app_from_script_origin(
    identity: PackageIdentity,
    origin: Dict[str, str],
    runtime_config: Dict[str, Any],
    *,
    refresh_app: bool,
) -> None:
    """Run a package-local origin script that populates ``App/`` from JSON on stdin."""
    script_path = _origin_script_path(identity, origin["script"], context="origin")
    if refresh_app:
        _clear_app(identity)

    # Each script type gets its native interpreter; the script runs from its
    # own directory and receives the package context as JSON.
    log_info(f"Running origin script: {origin['script']}")
    extension = script_path.suffix.lower()
    if extension == ".ps1":
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    elif extension in {".cmd", ".bat"}:
        command = ["cmd.exe", "/c", str(script_path)]
    else:
        command = [str(script_path)]
    completed = subprocess.run(
        command,
        input=json.dumps(build_origin_script_payload(identity, runtime_config), ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=str(script_path.parent),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in completed.stdout.splitlines():
        log_info(line)
    for line in completed.stderr.splitlines():
        log_warning(line)
    if completed.returncode != 0:
        raise RuntimeError(f"Origin script failed with exit code {completed.returncode}")
    if not app_has_payload(identity):
        raise RuntimeError("Origin script completed but App is missing or empty")


def populate_app_from_module_origin(
    identity: PackageIdentity,
    origin: Dict[str, str],
    runtime_config: Dict[str, Any],
    *,
    refresh_app: bool,
) -> None:
    """Run a package-local Python origin module's ``populate_app(context)`` hook."""
    from .updates import load_package_module

    if refresh_app:
        _clear_app(identity)

    # Origin modules share the update-hook loader, including its isolated
    # namespace and API-version validation.
    log_info(f"Running origin module: {origin['module']}")
    module = load_package_module(identity, origin["module"])
    callback = getattr(module, "populate_app", None)
    if not callable(callback):
        raise RuntimeError("Origin module must define populate_app(context)")
    callback({"apiVersion": 1, **build_origin_script_payload(identity, runtime_config)})
    if not app_has_payload(identity):
        raise RuntimeError("Origin module completed but App is missing or empty")


def build_origin_script_payload(
    identity: PackageIdentity, runtime_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the JSON object passed to origin scripts on stdin and to origin modules."""
    version_root = identity.version_path.resolve()
    app = str((identity.version_path / "App").resolve())
    return {
        "config": {
            "name": identity.name,
            "version": identity.version,
            "localVersion": identity.local_version,
            "only_portable": runtime_config["only_portable"],
            "origin": runtime_config.get("origin"),
            "shortcut": runtime_config["shortcut"],
            "environment": runtime_config["environment"],
            "path": [{"value": value} for value in runtime_config["path"]],
            "bin": runtime_config["bin"],
        },
        "identity": {
            "name": identity.name,
            "version": identity.version,
            "localVersion": identity.local_version,
            "versionString": identity.version_string,
        },
        "PkgVars": {
            "PkgRoot": str(version_root),
            "App": app,
            "VersionRoot": str(version_root),
            "Icons": str(version_root / "Icons"),
            "Shortcuts": str(version_root / "Shortcuts"),
        },
        "paths": {"stageApp": app},
    }


# ---------------------------------------------------------------------------
# Read-only health checks
# ---------------------------------------------------------------------------


def validate_origin_health(
    identity: PackageIdentity, origin: Optional[Dict[str, Any]]
) -> List[str]:
    """Return errors for origin scripts or modules that are missing or unsafe.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that owns the references.
    origin : dict, optional
        Normalized origin, including its historical versions.

    Returns
    -------
    List[str]
        One message per invalid reference.
    """
    if origin is None:
        return []
    errors: List[str] = []
    sources = [("origin", origin)] + [
        (f"origin.versions[{index}]", item) for index, item in enumerate(origin.get("versions", []))
    ]
    for context, item in sources:
        if item.get("mode") == "script":
            try:
                _origin_script_path(identity, item["script"], context=context)
            except RuntimeError as exc:
                errors.append(str(exc))
        elif item.get("mode") == "module" and not (identity.version_path / item["module"]).is_file():
            errors.append(f"Origin module does not exist: {item['module']}")
    return errors


def validate_update_health(
    identity: PackageIdentity, update: Optional[Dict[str, Any]]
) -> List[str]:
    """Return errors for configured update modules that do not exist.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that owns the ``pkg.local`` hooks.
    update : dict, optional
        Normalized update configuration.

    Returns
    -------
    List[str]
        One message per missing check, payload, or install-step module.
    """
    if update is None:
        return []
    references = [
        item["module"]
        for item in (update["check"], update["payload"], *update["steps"])
        if item.get("mode") == "module"
    ]
    return [
        f"Update module does not exist: {module}"
        for module in references
        if not (identity.version_path / module).is_file()
    ]


# ---------------------------------------------------------------------------
# Filesystem helpers shared with update staging
# ---------------------------------------------------------------------------


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """Extract a zip archive after rejecting absolute, escaping, and symlink members."""
    resolved_destination = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = Path(member.filename)
            windows_member_path = PureWindowsPath(member.filename)
            if (
                member_path.is_absolute()
                or windows_member_path.is_absolute()
                or windows_member_path.drive
                or ".." in member_path.parts
                or ".." in windows_member_path.parts
                or not (destination / member_path).resolve().is_relative_to(resolved_destination)
            ):
                raise RuntimeError("Zip archive contains an unsafe path")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Zip archive contains an unsupported symlink")
        archive.extractall(destination)


def copy_zip_extract_mappings(
    archive_root: Path, app_path: Path, mappings: List[Dict[str, str]]
) -> None:
    """Copy selected archive paths into a new ``App/`` according to ZIP mappings.

    A mapping ``src`` is a ZIP-root wildcard; a trailing ``/`` copies a matched
    directory's contents rather than the directory itself. ``dest`` is
    relative to ``App``.
    """
    resolved_root = archive_root.resolve()
    resolved_app = app_path.resolve(strict=False)
    app_path.mkdir()

    # Apply each mapping independently so package authors can compose a runtime
    # tree from several archive directories without extracting unrelated files.
    for mapping in mappings:
        src = mapping["src"]
        copy_contents = src.endswith("/")
        matches = list(archive_root.glob(src[:-1] if copy_contents else src))
        if not matches:
            raise RuntimeError(f"Update ZIP source matched no archive paths: {src!r}")

        # Keep each selected source and destination inside their staging roots
        # even when wildcard expansion reaches unusual names.
        destination = app_path / mapping["dest"]
        if not destination.resolve(strict=False).is_relative_to(resolved_app):
            raise RuntimeError("Update ZIP destination escapes App")
        destination.mkdir(parents=True, exist_ok=True)
        for source in matches:
            if not source.resolve().is_relative_to(resolved_root):
                raise RuntimeError("Update ZIP source escapes the archive")
            if copy_contents:
                if not source.is_dir():
                    raise RuntimeError(f"Update ZIP source ending in '/' is not a directory: {src!r}")
                copy_directory_contents(source, destination)
            elif source.is_dir():
                shutil.copytree(source, destination / source.name, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination / source.name)


def copy_directory_contents(source: Path, destination: Path) -> None:
    """Copy the entries under one directory into an existing directory."""
    for entry in source.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, destination / entry.name)
        else:
            shutil.copy2(entry, destination / entry.name)


def _replace_app(version_path: Path, prepared_app: Path) -> None:
    """Replace ``<version>/App`` with a prepared tree, restoring the old one on failure."""
    app_path = version_path / "App"

    # Move a populated App aside rather than deleting it, so a failed move of
    # the prepared tree can restore the previous payload.
    backup_path = Path(tempfile.mkdtemp(prefix=".gupkg-old-App-", dir=str(version_path)))
    backup_path.rmdir()
    if app_path.exists():
        if any(app_path.iterdir()):
            shutil.move(str(app_path), str(backup_path))
        else:
            app_path.rmdir()
    try:
        shutil.move(str(prepared_app), str(app_path))
    except Exception:
        if backup_path.exists() and not app_path.exists():
            shutil.move(str(backup_path), str(app_path))
        raise
    if backup_path.exists():
        shutil.rmtree(backup_path)


def _clear_app(identity: PackageIdentity) -> None:
    """Remove ``App`` before a script or module repopulates it in place."""
    app_path = identity.version_path / "App"
    if not app_path.exists():
        return
    resolved_app = app_path.resolve(strict=False)
    if resolved_app.parent != identity.version_path.resolve() or resolved_app.name != "App":
        raise RuntimeError("Refusing to clear App outside the package version directory")
    shutil.rmtree(app_path)


def _origin_script_path(identity: PackageIdentity, script: str, *, context: str) -> Path:
    """Resolve and validate a package-local origin script reference."""
    raw_script = Path(script)
    if raw_script.is_absolute():
        raise RuntimeError(f"[{context}].script must be relative to the package version directory")
    script_path = (identity.version_path / raw_script).resolve()
    if not script_path.is_relative_to(identity.version_path.resolve()):
        raise RuntimeError(f"[{context}].script cannot escape the package version directory")
    if not script_path.is_file():
        raise RuntimeError(f"[{context}].script was not found")
    if script_path.suffix.lower() not in {".ps1", ".cmd", ".bat", ".exe"}:
        raise RuntimeError(f"[{context}].script has an unsupported extension")
    return script_path
