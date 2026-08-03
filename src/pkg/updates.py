"""Provide staging and state primitives for explicit package upgrades.

Update hooks are loaded only from the package-owned ``pkg.local`` tree. Candidate
versions are normalized, downloaded or populated into manager-owned work space,
validated, and prepared for an atomic commit by the public action coordinator.

Implementation Approach
-----------------------
Persistent timing state and temporary work paths live below ``.pkg`` in the
package root. Hook modules use isolated import names and disabled bytecode
writes, while staged version trees are checked before activation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Optional, Tuple

from .core import (
    ConfigValidationError,
    ExpansionMode,
    PackageIdentity,
    compare_package_versions,
    expand_text,
    is_version_directory_name,
    log_warning,
    read_toml_file,
    write_text_atomic,
)
from .dependencies import run_with_missing_dependencies
from .metadata import sync_config_metadata_text
from .origin import (
    _app_contains_entries,
    _copy_directory_contents,
    copy_zip_extract_mappings,
    safe_extract_zip,
)
from .github_releases import check_update as check_github_release


def _update_paths(root: Path) -> Dict[str, Path]:
    """Return the manager-owned paths used by package update operations."""
    base = root / ".pkg"
    return {
        "base": base,
        "work": base / "work",
        "locks": base / "locks",
        "state": base / "state" / "update.toml",
        "receipts": base / "receipts",
    }


def _toml_value(value: Any) -> str:
    """Render the limited scalar values persisted by update state."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _load_update_state(path: Path) -> Dict[str, Any]:
    """Load advisory update state, preserving a corrupt file for diagnosis."""
    if not path.exists():
        return {"assignedVersion": []}
    try:
        return read_toml_file(path)
    except Exception:
        backup = path.with_name(
            f"update.corrupt-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.toml"
        )
        path.replace(backup)
        log_warning(f"Corrupt update state was moved to {backup}")
        return {"assignedVersion": []}


def _write_update_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomically persist the small TOML update coordination document."""
    lines = ["schemaVersion = 1"]
    for key in (
        "lastAttemptedCheck",
        "lastSuccessfulCheck",
        "lastStatus",
        "lastCandidateId",
        "lastError",
    ):
        if state.get(key) is not None:
            lines.append(f"{key} = {_toml_value(state[key])}")
    for item in state.get("assignedVersion", [])[-100:]:
        lines.extend(
            [
                "",
                "[[assignedVersion]]",
                f"candidateId = {_toml_value(item['candidateId'])}",
                f"version = {_toml_value(item['version'])}",
            ]
        )
    write_text_atomic(path, "\n".join(lines) + "\n")


def _load_package_module(identity: PackageIdentity, reference: str, pycache: Path):
    """Load one trusted package-local module without retaining its namespace."""
    path = (identity.version_path / reference).resolve()
    local_root = (identity.version_path / "pkg.local").resolve()
    if not path.exists():
        raise ConfigValidationError(f"Package-local module does not exist: {reference}")
    name = f"_pkg_local_{hashlib.sha256((str(path) + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:16]}"
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(local_root)]
    )
    if spec is None or spec.loader is None:
        raise ConfigValidationError(f"Cannot load package-local module: {reference}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.modules[name] = module
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
        # Remove this module family only; package-local relative imports use the
        # same unique prefix and must not leak state into later update actions.
        for imported_name in list(sys.modules):
            if imported_name == name or imported_name.startswith(name + "."):
                sys.modules.pop(imported_name, None)
    if getattr(module, "PKG_MODULE_API", None) != 1:
        raise ConfigValidationError(f"{reference} must declare PKG_MODULE_API = 1")
    return module


def _candidate_version(state: Dict[str, Any], candidate_id: str) -> str:
    """Reuse the UTC version assigned to a Git candidate."""
    for assigned in state.get("assignedVersion", []):
        if assigned.get("candidateId") == candidate_id:
            return assigned["version"]
    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-git")
    state.setdefault("assignedVersion", []).append(
        {"candidateId": candidate_id, "version": version}
    )
    return version


def _normalize_update_candidate(
    raw: Dict[str, Any], identity: PackageIdentity, state: Dict[str, Any], mode: str
) -> Dict[str, Any]:
    """Validate a discovered update before it is allowed to reach staging."""
    if not isinstance(raw, dict):
        raise ConfigValidationError("Update check must return a mapping or None")
    required = {"candidateId", "version"} | (
        {"url"} if mode in {"github", "module"} else set()
    )
    if not required <= set(raw):
        raise ConfigValidationError(
            f"Update candidate is missing: {', '.join(sorted(required - set(raw)))}"
        )
    candidate_id, version = raw["candidateId"], raw["version"]
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or not isinstance(version, str)
        or not version.strip()
    ):
        raise ConfigValidationError(
            "Update candidate ID and version must be non-empty strings"
        )
    if (
        "/" in version
        or "\\" in version
        or ".." in version
        or not is_version_directory_name(f"v{version}.l1")
    ):
        raise ConfigValidationError(
            "Update candidate version is unsafe for a version directory"
        )
    comparison = compare_package_versions(version, identity.version)
    is_bootstrap = identity.version.startswith("bootstrap")
    if not is_bootstrap and comparison < 0:
        raise ConfigValidationError("Update candidate is older than the active version")
    if (
        not is_bootstrap
        and comparison == 0
        and candidate_id != state.get("lastCandidateId")
    ):
        raise ConfigValidationError(
            "A different candidate cannot republish the active version"
        )
    result = dict(raw)
    result["candidateId"] = candidate_id
    result["version"] = version
    return result


def _check_update(
    identity: PackageIdentity,
    config: Dict[str, Any],
    state: Dict[str, Any],
    work: Path,
    *,
    local_deps_autoinstall: bool = False,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Discover the current or next upstream state without changing App."""
    update = config["update"]
    check = update["check"]
    if check["mode"] == "git":
        app = (identity.version_path / check["appPath"]).resolve()
        if not app.is_relative_to(identity.version_path.resolve()):
            raise ConfigValidationError("Git appPath escapes the version directory")
        local = subprocess.run(
            ["git", "-C", str(app), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        origin = config.get("origin")
        if origin is not None and origin.get("mode") == "git":
            candidate = _git_origin_candidate(identity, config, state)
            if local == candidate["commit"]:
                return "current", None
            return "available", candidate
        else:
            url = subprocess.run(
                ["git", "-C", str(app), "remote", "get-url", check["remote"]],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        remote = subprocess.run(
            ["git", "ls-remote", "--exit-code", url, check["ref"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        if local == remote:
            return "current", None
        candidate_id = f"git:{remote}"
        version = _candidate_version(state, candidate_id)
        return "available", {
            "candidateId": candidate_id,
            "version": version,
            "url": url,
            "ref": check["ref"],
            "commit": remote,
        }
    context = {
        "apiVersion": 1,
        "current": {
            "name": identity.name,
            "version": identity.version,
            "localVersion": identity.local_version,
            "versionString": identity.version_string,
            "candidateId": state.get("lastCandidateId"),
        },
        "paths": {
            "packageRoot": identity.package_root,
            "versionRoot": identity.version_path,
            "app": identity.version_path / "App",
        },
        "state": dict(state),
    }
    if check["mode"] == "github":
        context.update(
            {
                "url": check["url"],
                "assetName": check["assetName"],
            }
        )
        callback = check_github_release
    else:
        module = run_with_missing_dependencies(
            _load_package_module,
            identity,
            check["module"],
            work / "pycache",
            autoinstall=local_deps_autoinstall,
        )
        callback = getattr(module, "check_update", None)
        if not callable(callback):
            raise ConfigValidationError(
                "Update check module must define check_update(context)"
            )
        context["channel"] = check["channel"]
    raw = run_with_missing_dependencies(
        callback, context, autoinstall=local_deps_autoinstall
    )
    if raw is None:
        return "current", None
    return "available", _normalize_update_candidate(
        raw, identity, state, check["mode"]
    )


def _git_origin_candidate(
    identity: PackageIdentity,
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the exact candidate declared by a configured Git origin."""
    origin = config.get("origin")
    check = config["update"]["check"]
    if origin is None or origin.get("mode") != "git":
        raise ConfigValidationError("Git bootstrap requires a configured Git origin")
    if origin["ref"] != check["ref"]:
        raise ConfigValidationError("Git origin and update check must use the same ref")

    remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", origin["url"], origin["ref"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    candidate_id = f"git:{remote}"
    return {
        "candidateId": candidate_id,
        "version": _candidate_version(state, candidate_id),
        "url": origin["url"],
        "ref": origin["ref"],
        "commit": remote,
    }


def _next_version_identity(
    identity: PackageIdentity, candidate: Dict[str, Any]
) -> PackageIdentity:
    """Assign a candidate local revision, pinning bootstrap promotions to ``.l1``."""
    if identity.version.startswith("bootstrap"):
        path = identity.package_root / f"v{candidate['version']}.l1"
        return PackageIdentity.from_version_path(
            identity.package_root, path, is_current=False
        )

    revision = 1
    while (identity.package_root / f"v{candidate['version']}.l{revision}").exists():
        revision += 1
    path = identity.package_root / f"v{candidate['version']}.l{revision}"
    return PackageIdentity.from_version_path(
        identity.package_root, path, is_current=False
    )


def _prepare_update(
    identity: PackageIdentity,
    config: Dict[str, Any],
    candidate: Dict[str, Any],
    work: Path,
    *,
    no_checksum: bool,
    local_deps_autoinstall: bool = False,
) -> PackageIdentity:
    """Build a complete new version under work before a single final rename."""
    new_identity = _next_version_identity(identity, candidate)
    stage = work / "version"
    stage.mkdir(parents=True)
    shutil.copytree(
        identity.version_path,
        stage,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("App", "__pycache__", "*.pyc"),
    )
    staged_identity = PackageIdentity.from_version_path(
        identity.package_root,
        stage.with_name(new_identity.version_string),
        is_current=False,
    )
    text = (stage / "pkg.toml").read_text(encoding="utf-8")
    rendered, _ = sync_config_metadata_text(text, staged_identity)
    write_text_atomic(stage / "pkg.toml", rendered)
    stage_app = stage / "App"
    payload = config["update"]["payload"]
    if payload["mode"] == "git":
        subprocess.run(
            ["git", "clone", "--no-checkout", candidate["url"], str(stage_app)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(stage_app), "checkout", "--detach", candidate["commit"]],
            check=True,
        )
    else:
        url = candidate["url"]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ConfigValidationError("Update URL must be credential-free HTTP(S)")
        headers = candidate.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
                for name, value in headers.items()
            )
        ):
            raise ConfigValidationError("Update candidate headers must be safe strings")
        download = work / "download"
        download.mkdir()
        artifact = download / "payload.part"
        request = urllib.request.Request(url, headers=headers) if headers else url
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            open(artifact, "wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
        checksum = candidate.get("sha256")
        bypass = (
            "cli-bypass"
            if no_checksum
            else ("version-ignore" if payload["ignore_checksum"] else None)
        )
        if not bypass:
            if (
                not isinstance(checksum, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", checksum) is None
            ):
                raise ConfigValidationError(
                    "Update candidate requires a sha256 checksum"
                )
            if (
                hashlib.sha256(artifact.read_bytes()).hexdigest().lower()
                != checksum.lower()
            ):
                raise RuntimeError("Update checksum did not match downloaded file")
        else:
            log_warning(
                f"Checksum verification bypassed for {candidate['version']} ({bypass})"
            )
        if payload["mode"] == "zip":
            file_name = candidate.get("fileName")
            if not isinstance(file_name, str):
                file_name = Path(
                    urllib.parse.unquote(urllib.parse.urlparse(candidate["url"]).path)
                ).name
            if isinstance(file_name, str) and file_name.lower().endswith(".exe"):
                # A release executable is already the complete application payload.
                # Preserve its published name while keeping it inside the staged App.
                if (
                    Path(file_name).name != file_name
                    or PureWindowsPath(file_name).name != file_name
                ):
                    raise ConfigValidationError("Update executable fileName must be a name")
                if payload.get("extract") or payload.get("extractSubdir"):
                    raise ConfigValidationError(
                        "Update executable payloads cannot use ZIP extraction settings"
                    )
                stage_app.mkdir()
                shutil.copy2(artifact, stage_app / file_name)
            else:
                extract = work / "extract"
                extract.mkdir()
                safe_extract_zip(artifact, extract)
                mappings = payload.get("extract")
                if mappings:
                    copy_zip_extract_mappings(extract, stage_app, mappings)
                else:
                    source = extract / candidate.get(
                        "extractSubdir", payload.get("extractSubdir", "")
                    )
                    if not source.exists():
                        source = extract
                    if not source.is_dir() or not source.resolve().is_relative_to(
                        extract.resolve()
                    ):
                        raise RuntimeError("Update extractSubdir was not found safely")
                    stage_app.mkdir()
                    _copy_directory_contents(source, stage_app)
            _apply_payload_renames(
                stage_app,
                payload.get("rename", []),
                staged_identity,
            )
        else:
            module = run_with_missing_dependencies(
                _load_package_module,
                identity,
                payload["module"],
                work / "pycache",
                autoinstall=local_deps_autoinstall,
            )
            callback = getattr(module, "unpack_app", None)
            if not callable(callback):
                raise ConfigValidationError(
                    "Update unpack module must define unpack_app(context)"
                )
            run_with_missing_dependencies(
                callback,
                {
                    "apiVersion": 1,
                    "candidate": dict(candidate),
                    "paths": {
                        "artifact": artifact,
                        "stageRoot": stage,
                        "stageApp": stage_app,
                    },
                },
                autoinstall=local_deps_autoinstall,
            )
    if not _app_contains_entries(stage_app):
        raise RuntimeError("Prepared update App directory is missing or empty")
    return new_identity


def _apply_payload_renames(
    app_path: Path,
    mappings: list[Dict[str, str]],
    identity: PackageIdentity,
) -> None:
    """Rename configured files or directories within a staged ``App`` tree."""
    resolved_app = app_path.resolve()

    # Expand the release version only after the candidate identity is known,
    # while retaining containment checks before every filesystem mutation.
    for mapping in mappings:
        source_text = expand_text(
            mapping["src"], identity, ExpansionMode.GENERAL
        ).value
        destination_text = expand_text(
            mapping["dest"], identity, ExpansionMode.GENERAL
        ).value
        source = app_path / source_text
        destination = app_path / destination_text
        resolved_source = source.resolve()
        resolved_destination = destination.resolve(strict=False)
        if (
            not resolved_source.is_relative_to(resolved_app)
            or not resolved_destination.is_relative_to(resolved_app)
            or resolved_source == resolved_app
            or resolved_destination == resolved_app
        ):
            raise RuntimeError("Update rename path escapes App")
        if not source.exists():
            raise RuntimeError(f"Update rename source was not found: {mapping['src']!r}")
        if destination.exists():
            raise RuntimeError(
                f"Update rename destination already exists: {mapping['dest']!r}"
            )
        if source.is_dir() and resolved_destination.is_relative_to(resolved_source):
            raise RuntimeError("Update rename destination cannot be inside its source")

        # Make intentional subdirectory renames possible without allowing a
        # package to replace an existing staged file or directory.
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
