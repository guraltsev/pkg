"""Provide staging and state primitives for automatic package updates.

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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .core import (
    ConfigValidationError,
    PackageIdentity,
    compare_package_versions,
    is_version_directory_name,
    log_warning,
    read_toml_file,
    write_text_atomic,
)
from .metadata import sync_config_metadata_text
from .origin import _app_contains_entries, _copy_directory_contents, safe_extract_zip


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
    required = {"candidateId", "version"} | ({"url"} if mode == "module" else set())
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
    if identity.version != "bootstrap" and comparison < 0:
        raise ConfigValidationError("Update candidate is older than the active version")
    if (
        identity.version != "bootstrap"
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
    identity: PackageIdentity, config: Dict[str, Any], state: Dict[str, Any], work: Path
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
    module = _load_package_module(identity, check["module"], work / "pycache")
    callback = getattr(module, "check_update", None)
    if not callable(callback):
        raise ConfigValidationError(
            "Update check module must define check_update(context)"
        )
    context = {
        "apiVersion": 1,
        "channel": check["channel"],
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
    raw = callback(context)
    if raw is None:
        return "current", None
    return "available", _normalize_update_candidate(raw, identity, state, "module")


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
    """Assign the first unused local revision for a prepared candidate."""
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
        download = work / "download"
        download.mkdir()
        artifact = download / "payload.part"
        with (
            urllib.request.urlopen(url, timeout=60) as response,
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
            extract = work / "extract"
            extract.mkdir()
            safe_extract_zip(artifact, extract)
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
        else:
            module = _load_package_module(identity, payload["module"], work / "pycache")
            callback = getattr(module, "unpack_app", None)
            if not callable(callback):
                raise ConfigValidationError(
                    "Update unpack module must define unpack_app(context)"
                )
            callback(
                {
                    "apiVersion": 1,
                    "candidate": dict(candidate),
                    "paths": {
                        "artifact": artifact,
                        "stageRoot": stage,
                        "stageApp": stage_app,
                    },
                }
            )
    if not _app_contains_entries(stage_app):
        raise RuntimeError("Prepared update App directory is missing or empty")
    return new_identity


def _refresh_git_app_inplace(
    identity: PackageIdentity,
    config: Dict[str, Any],
    candidate: Dict[str, Any],
) -> None:
    """Fast-forward an in-place Git checkout to a checked candidate."""
    check = config["update"]["check"]
    app = (identity.version_path / check["appPath"]).resolve()
    if not app.is_relative_to(identity.version_path.resolve()):
        raise ConfigValidationError("Git appPath escapes the version directory")

    # Preserve tracked local work instead of allowing an update to mix it with
    # a different upstream revision. Untracked runtime files remain untouched.
    status = subprocess.run(
        ["git", "-C", str(app), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        raise RuntimeError(
            "Git-inplace update refused because App has tracked local changes"
        )

    # Fetch only the configured ref and verify it still names the commit found
    # by the preceding check before changing the checkout.
    subprocess.run(
        [
            "git",
            "-C",
            str(app),
            "fetch",
            "--no-tags",
            candidate["url"],
            check["ref"],
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    fetched = subprocess.run(
        ["git", "-C", str(app), "rev-parse", "FETCH_HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if fetched != candidate["commit"]:
        raise RuntimeError(
            "Configured Git ref changed during the rolling update; retry the update"
        )

    # A fast-forward merge retains the checkout's branch or detached-HEAD
    # state while refusing divergent local commits.
    subprocess.run(
        ["git", "-C", str(app), "merge", "--ff-only", candidate["commit"]],
        capture_output=True,
        text=True,
        check=True,
    )
