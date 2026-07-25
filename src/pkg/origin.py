"""Populate application payloads from declared package origins.

Git, zip, and script origins prepare a complete temporary application tree
before replacing ``App/``. Source refs, checksums, archive paths, and
package-local script references are validated before the existing payload is
mutated.

Implementation Approach
-----------------------
Origin selection is performed by normalized configuration. Clones, downloads,
and scripts write into temporary staging directories; validated contents are
then moved into place with recovery of the previous application directory on
error.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from .core import PackageIdentity, StepResult, log_error, log_info, log_warning


def _app_contains_entries(app_path: Path) -> bool:
    """Return whether ``App/`` exists and contains at least one entry."""
    return app_path.exists() and any(app_path.iterdir())


def app_needs_origin_population(identity: PackageIdentity, refresh_app: bool) -> bool:
    """Return whether the selected package version needs ``App/`` population."""
    app_path = identity.version_path / "App"
    return refresh_app or not _app_contains_entries(app_path)


def populate_app_from_origin(
    identity: PackageIdentity,
    runtime_config: Dict[str, Any],
    *,
    no_checksum: bool = False,
    refresh_app: bool = False,
) -> StepResult:
    """Populate ``App/`` from the package origin when the install requires it."""
    origin = runtime_config.get("origin")
    app_path = identity.version_path / "App"
    if origin is None:
        return StepResult(ok=True, changed=False)

    if not app_needs_origin_population(identity, refresh_app):
        log_info("App is already populated; skipping origin population")
        return StepResult(ok=True, changed=False)

    if refresh_app and _app_contains_entries(app_path):
        log_info("--refresh-app enabled; clearing App before origin population")
    elif app_path.exists():
        log_info("App is empty; populating from origin...")
    else:
        log_info("App is missing; populating from origin...")

    try:
        if "mode" not in origin:
            origin_version = origin.get("version", "unknown")
            raise RuntimeError(
                f"Origin version '{origin_version}' does not declare url or script, so it cannot populate App"
            )
        if origin["mode"] == "zip":
            populate_app_from_zip_origin(identity, origin, no_checksum=no_checksum)
        elif origin["mode"] == "git":
            populate_app_from_git_origin(identity, origin)
        elif origin["mode"] == "script":
            populate_app_from_script_origin(
                identity, origin, runtime_config, refresh_app=refresh_app
            )
        else:
            return StepResult(
                ok=False, errors=[f"Unsupported origin mode: {origin['mode']}"]
            )
    except Exception as exc:
        message = str(exc)
        log_error(message)
        return StepResult(ok=False, changed=False, errors=[message])
    return StepResult(ok=True, changed=True)


def populate_app_from_git_origin(
    identity: PackageIdentity, origin: Dict[str, str]
) -> None:
    """Populate ``App/`` with the exact commit at a configured Git ref."""
    app_path = identity.version_path / "App"
    with tempfile.TemporaryDirectory(
        prefix=".pkg-origin-", dir=str(identity.version_path)
    ) as temp_root_name:
        prepared_app = Path(temp_root_name) / "App.new"

        # Resolve the configured ref before cloning so the installed checkout
        # records one exact source state even if the branch advances.
        log_info(f"Cloning Git origin: {origin['url']} ({origin['ref']})")
        remote = subprocess.run(
            ["git", "ls-remote", "--exit-code", origin["url"], origin["ref"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        subprocess.run(
            ["git", "clone", "--no-checkout", origin["url"], str(prepared_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(prepared_app),
                "fetch",
                "--no-tags",
                "origin",
                origin["ref"],
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        fetched = subprocess.run(
            ["git", "-C", str(prepared_app), "rev-parse", "FETCH_HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if fetched != remote:
            raise RuntimeError(
                "Configured Git ref changed during origin population; retry installation"
            )
        subprocess.run(
            ["git", "-C", str(prepared_app), "checkout", "--detach", fetched],
            capture_output=True,
            text=True,
            check=True,
        )
        _replace_app_directory(identity.version_path, app_path, prepared_app)


def populate_app_from_zip_origin(
    identity: PackageIdentity, origin: Dict[str, str], *, no_checksum: bool
) -> None:
    """Populate ``App/`` from a downloaded zip archive."""
    app_path = identity.version_path / "App"
    with tempfile.TemporaryDirectory(
        prefix=".pkg-origin-", dir=str(identity.version_path)
    ) as temp_root_name:
        temp_root = Path(temp_root_name)
        archive_path = temp_root / "origin.zip"
        staging_dir = temp_root / "extract"
        prepared_app = temp_root / "App.new"
        staging_dir.mkdir()

        log_info(f"Downloading origin: {origin['url']}")
        with urllib.request.urlopen(origin["url"]) as response:
            with open(archive_path, "wb") as file_handle:
                shutil.copyfileobj(response, file_handle)

        checksum = origin.get("checksum")
        if checksum and no_checksum:
            log_warning(
                "Checksum verification skipped because --no-checksum was provided"
            )
        elif checksum:
            log_info("Verifying sha256 checksum...")
            expected = checksum.split(":", 1)[1].lower()
            digest = hashlib.sha256()
            with open(archive_path, "rb") as file_handle:
                for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected:
                raise RuntimeError("[origin].checksum did not match downloaded file")

        log_info("Extracting zip archive...")
        safe_extract_zip(archive_path, staging_dir)

        selected_source = staging_dir
        extract_subdir = origin.get("extractSubdir")
        if extract_subdir:
            log_info(f"Using archive subdirectory: {extract_subdir}")
            selected_source = staging_dir / extract_subdir
        resolved_source = selected_source.resolve()
        resolved_staging = staging_dir.resolve()
        if not resolved_source.is_relative_to(resolved_staging):
            raise RuntimeError("[origin].extractSubdir cannot escape the archive")
        if not selected_source.exists() or not selected_source.is_dir():
            raise RuntimeError("[origin].extractSubdir was not found in the archive")

        prepared_app.mkdir()
        _copy_directory_contents(selected_source, prepared_app)
        _replace_app_directory(identity.version_path, app_path, prepared_app)


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """Extract a zip archive after rejecting unsafe members."""
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
            ):
                raise RuntimeError("Zip archive contains an unsafe path")
            resolved_member_path = (destination / member_path).resolve()
            if not resolved_member_path.is_relative_to(resolved_destination):
                raise RuntimeError("Zip archive contains an unsafe path")
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise RuntimeError("Zip archive contains an unsupported symlink")
        archive.extractall(destination)


def copy_zip_extract_mappings(
    archive_root: Path, app_path: Path, mappings: List[Dict[str, str]]
) -> None:
    """Copy selected archive paths into ``App/`` according to ZIP mappings."""
    resolved_root = archive_root.resolve()
    resolved_app = app_path.resolve(strict=False)
    app_path.mkdir()

    # Apply each mapping independently so package authors can compose a runtime
    # tree from several archive directories without extracting unrelated files.
    for mapping in mappings:
        src = mapping["src"]
        copy_contents = src.endswith("/")
        pattern = src[:-1] if copy_contents else src
        matches = list(archive_root.glob(pattern))
        if not matches:
            raise RuntimeError(f"Update ZIP source matched no archive paths: {src!r}")

        # Keep each selected source and destination inside their respective
        # staging roots even when wildcard expansion reaches unusual names.
        destination = app_path / mapping["dest"]
        resolved_destination = destination.resolve(strict=False)
        if not resolved_destination.is_relative_to(resolved_app):
            raise RuntimeError("Update ZIP destination escapes App")
        destination.mkdir(parents=True, exist_ok=True)
        for source in matches:
            resolved_source = source.resolve()
            if not resolved_source.is_relative_to(resolved_root):
                raise RuntimeError("Update ZIP source escapes the archive")
            if copy_contents:
                if not source.is_dir():
                    raise RuntimeError(
                        f"Update ZIP source ending in '/' is not a directory: {src!r}"
                    )
                _copy_directory_contents(source, destination)
            elif source.is_dir():
                shutil.copytree(source, destination / source.name, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination / source.name)


def _copy_directory_contents(source: Path, destination: Path) -> None:
    """Copy the entries under one directory into another directory."""
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def _replace_app_directory(
    version_path: Path, app_path: Path, prepared_app: Path
) -> None:
    """Replace ``App/`` with already prepared contents."""
    resolved_version = version_path.resolve()
    resolved_app = app_path.resolve(strict=False)
    if resolved_app.parent != resolved_version or resolved_app.name != "App":
        raise RuntimeError(
            "Refusing to replace App outside the package version directory"
        )

    backup_path = Path(tempfile.mkdtemp(prefix=".pkg-old-App-", dir=str(version_path)))
    backup_path.rmdir()
    if app_path.exists():
        if _app_contains_entries(app_path):
            shutil.move(str(app_path), str(backup_path))
        else:
            shutil.rmtree(app_path)
    try:
        shutil.move(str(prepared_app), str(app_path))
    except Exception:
        if backup_path.exists() and not app_path.exists():
            shutil.move(str(backup_path), str(app_path))
        raise
    if backup_path.exists():
        shutil.rmtree(backup_path)


def populate_app_from_script_origin(
    identity: PackageIdentity,
    origin: Dict[str, str],
    runtime_config: Dict[str, Any],
    *,
    refresh_app: bool,
) -> None:
    """Run a package-local origin script and verify that it populated ``App/``."""
    script_path = _resolve_origin_script_path(identity, origin["script"])
    app_path = identity.version_path / "App"
    if refresh_app and app_path.exists():
        resolved_app = app_path.resolve(strict=False)
        if (
            resolved_app.parent != identity.version_path.resolve()
            or resolved_app.name != "App"
        ):
            raise RuntimeError(
                "Refusing to clear App outside the package version directory"
            )
        shutil.rmtree(app_path)

    log_info(f"Running origin script: {origin['script']}")
    payload = json.dumps(
        build_origin_script_payload(identity, runtime_config), ensure_ascii=False
    )
    extension = script_path.suffix.lower()
    if extension == ".ps1":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
    elif extension in {".cmd", ".bat"}:
        command = ["cmd.exe", "/c", str(script_path)]
    else:
        command = [str(script_path)]

    completed = subprocess.run(
        command,
        input=payload,
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
        raise RuntimeError(
            f"Origin script failed with exit code {completed.returncode}"
        )
    if not _app_contains_entries(app_path):
        raise RuntimeError("Origin script completed but App is missing or empty")


def _resolve_origin_script_path(identity: PackageIdentity, script: str) -> Path:
    """Resolve and validate a package-local origin script path."""
    return _validate_origin_script_reference(identity, script, context="origin")


def _validate_origin_script_reference(
    identity: PackageIdentity, script: str, *, context: str
) -> Path:
    """Validate and resolve an origin script reference."""
    raw_script = Path(script)
    if raw_script.is_absolute():
        raise RuntimeError(
            f"[{context}].script must be relative to the package version directory"
        )
    script_path = (identity.version_path / raw_script).resolve()
    if not script_path.is_relative_to(identity.version_path.resolve()):
        raise RuntimeError(
            f"[{context}].script cannot escape the package version directory"
        )
    if not script_path.exists() or not script_path.is_file():
        raise RuntimeError(f"[{context}].script was not found")
    if script_path.suffix.lower() not in {".ps1", ".cmd", ".bat", ".exe"}:
        raise RuntimeError(f"[{context}].script has an unsupported extension")
    return script_path


def build_origin_script_payload(
    identity: PackageIdentity, runtime_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the JSON object passed to origin scripts on stdin."""
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
            "PkgRoot": str(identity.version_path.resolve()),
            "App": str((identity.version_path / "App").resolve()),
            "Icons": str((identity.version_path / "Icons").resolve()),
            "Shortcuts": str((identity.version_path / "Shortcuts").resolve()),
        },
    }


def validate_origin_health(
    identity: PackageIdentity, origin: Optional[Dict[str, Any]]
) -> List[str]:
    """Return origin configuration health-check errors."""
    if origin is None:
        return []

    errors: List[str] = []
    origin_sources: List[Tuple[str, Dict[str, Any]]] = [("origin", origin)]
    for index, item in enumerate(origin.get("versions", [])):
        origin_sources.append((f"origin.versions[{index}]", item))

    for context, item in origin_sources:
        if item.get("mode") == "script":
            try:
                _validate_origin_script_reference(
                    identity, item.get("script", ""), context=context
                )
            except RuntimeError as exc:
                errors.append(str(exc))
    return errors


def validate_update_health(
    identity: PackageIdentity, update: Optional[Dict[str, Any]]
) -> List[str]:
    """Return update-hook reference errors without contacting an update source."""
    if update is None:
        return []
    references = [update["check"]] if update["check"]["mode"] == "module" else []
    if update["payload"]["mode"] == "module":
        references.append(update["payload"])
    errors: List[str] = []
    for item in references:
        path = identity.version_path / item["module"]
        if not path.exists() or not path.is_file():
            errors.append(f"Update module does not exist: {item['module']}")
    return errors
