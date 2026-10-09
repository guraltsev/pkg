"""Provide staging and state primitives for explicit package upgrades.

Update hooks are loaded only from the package-owned ``pkg.local`` tree. Candidate
versions are normalized, downloaded or populated into manager-owned work space,
validated, and prepared for an atomic commit by the public action coordinator in
:mod:`gupkg.gupkg`.

Usage and API
-------------
``check_update(...)`` discovers the current or next upstream candidate,
``prepare_update(...)`` stages a complete version tree, and
``load_update_state(...)``, ``write_update_state(...)``, and
``write_receipt(...)`` persist the package-root coordination files located by
``update_paths(...)``. ``load_package_module(...)`` imports one trusted
``pkg.local`` hook.

Implementation Approach
-----------------------
Persistent timing state, receipts, locks, and temporary work paths live below
``.gupkg`` in the package root. Hook modules use isolated import names and
disabled bytecode writes. A staged version is built entirely inside the work
directory and checked for a non-empty ``App`` before the coordinator renames it
into place.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
from .downloads import download_response, file_sha256
from .github_releases import check_update as check_github_release
from .metadata import sync_config_metadata_text
from .origin import (
    app_has_payload,
    copy_directory_contents,
    copy_zip_extract_mappings,
    safe_extract_zip,
)


def update_paths(root: Path) -> Dict[str, Path]:
    """Return the manager-owned paths used by package update operations.

    Parameters
    ----------
    root : Path
        Package root that owns update state and temporary work.

    Returns
    -------
    Dict[str, Path]
        Paths for the update state, locks, receipts, and staging workspace.
    """
    base = root / ".gupkg"
    return {
        "base": base,
        "work": base / "work",
        "locks": base / "locks",
        "state": base / "state" / "update.toml",
        "receipts": base / "receipts",
    }


# ---------------------------------------------------------------------------
# Persistent state and receipts
# ---------------------------------------------------------------------------


def load_update_state(path: Path) -> Dict[str, Any]:
    """Load advisory update state, preserving a corrupt file for diagnosis.

    Parameters
    ----------
    path : Path
        TOML state file owned by one package root.

    Returns
    -------
    Dict[str, Any]
        Parsed state, or an initialized state mapping when the file is absent
        or was moved aside because it was corrupt.
    """
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


def write_update_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomically persist the small TOML update coordination document.

    Parameters
    ----------
    path : Path
        State file to replace.
    state : Dict[str, Any]
        Timing, last-result, and assigned Git-version records; only the most
        recent 100 assignments are retained.
    """
    lines = ["schemaVersion = 1"]
    for key in (
        "lastAttemptedCheck",
        "lastSuccessfulCheck",
        "lastStatus",
        "lastCandidateId",
        "lastCandidateVersion",
        "lastError",
    ):
        if state.get(key) is not None:
            lines.append(f"{key} = {_toml_string(state[key])}")
    for item in state.get("assignedVersion", [])[-100:]:
        lines.extend([
            "",
            "[[assignedVersion]]",
            f"candidateId = {_toml_string(item['candidateId'])}",
            f"version = {_toml_string(item['version'])}",
        ])
    write_text_atomic(path, "\n".join(lines) + "\n")


def write_receipt(receipt: Path, candidate: Dict[str, Any], identity: PackageIdentity) -> None:
    """Record that *identity* is a staged, not yet activated, update.

    Parameters
    ----------
    receipt : Path
        Receipt file below the package root's ``receipts`` directory.
    candidate : Dict[str, Any]
        Candidate whose payload was staged.
    identity : PackageIdentity
        Committed version directory that the next activation should install.
    """
    write_text_atomic(
        receipt,
        "schemaVersion = 1\n"
        f"candidateId = {_toml_string(candidate['candidateId'])}\n"
        f"version = {_toml_string(identity.version)}\n"
        f"localVersion = {identity.local_version}\n",
    )


def _toml_string(value: Any) -> str:
    """Render one value as a TOML basic string."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def check_update(
    identity: PackageIdentity,
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    local_deps_autoinstall: bool = False,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Discover the current or next upstream state without changing ``App``.

    Parameters
    ----------
    identity : PackageIdentity
        Package version whose configured source should be checked.
    config : Dict[str, Any]
        Normalized package configuration containing the update declaration.
    state : Dict[str, Any]
        Mutable package-owned update state used for candidate continuity.
    local_deps_autoinstall : bool, default=False
        Whether trusted package-local hooks may install missing imports.

    Returns
    -------
    Tuple[str, Optional[Dict[str, Any]]]
        ``("current", None)`` or ``("available", candidate)``.
    """
    check = config["update"]["check"]
    if check["mode"] == "git":
        return _check_git(identity, config, state)

    # GitHub and module checks share the hook context; the built-in GitHub
    # checker additionally receives its repository and asset settings.
    context: Dict[str, Any] = {
        "apiVersion": 1,
        "current": {
            "name": identity.name,
            "version": identity.version,
            "localVersion": identity.local_version,
            "versionString": identity.version_string,
            "candidateId": state.get("lastCandidateId"),
            "appReady": app_has_payload(identity),
        },
        "paths": {
            "packageRoot": identity.package_root,
            "versionRoot": identity.version_path,
            "app": identity.version_path / "App",
            "payload": identity.version_path / "App",
        },
        "state": dict(state),
    }
    if check["mode"] == "github":
        context.update({
            "url": check["url"],
            "assetName": check["assetName"],
            "tagPrefix": check.get("tagPrefix"),
        })
        callback = check_github_release
    else:
        context["channel"] = check["channel"]
        callback = _hook(identity, check["module"], "check_update", local_deps_autoinstall)
    raw = run_with_missing_dependencies(callback, context, autoinstall=local_deps_autoinstall)
    if raw is None:
        return "current", None
    return "available", _normalize_update_candidate(raw, identity, state)


def git_origin_candidate(
    identity: PackageIdentity,
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the exact candidate declared by a configured Git origin.

    Parameters
    ----------
    identity : PackageIdentity
        Bootstrap or package identity used for candidate version assignment.
    config : Dict[str, Any]
        Normalized configuration containing matching Git origin and check refs.
    state : Dict[str, Any]
        Mutable update state used to reuse assigned candidate versions.

    Returns
    -------
    Dict[str, Any]
        Candidate metadata suitable for staging.
    """
    origin = config.get("origin")
    if origin is None or origin.get("mode") != "git":
        raise ConfigValidationError("Git bootstrap requires a configured Git origin")
    if origin["ref"] != config["update"]["check"]["ref"]:
        raise ConfigValidationError("Git origin and update check must use the same ref")
    commit = _git("ls-remote", "--exit-code", origin["url"], origin["ref"]).split()[0]
    return _git_candidate(identity, state, origin["url"], origin["ref"], commit)


def _check_git(
    identity: PackageIdentity, config: Dict[str, Any], state: Dict[str, Any]
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Compare a Git checkout's ``HEAD`` with its upstream ref."""
    check = config["update"]["check"]
    app = (identity.version_path / check["appPath"]).resolve()
    if not app.is_relative_to(identity.version_path.resolve()):
        raise ConfigValidationError("Git appPath escapes the version directory")

    # A Git origin names the upstream directly, so a missing checkout is
    # simply a repairable payload rather than an error.
    origin = config.get("origin")
    if origin is not None and origin.get("mode") == "git":
        candidate = git_origin_candidate(identity, config, state)
        if app.is_dir() and _git("-C", str(app), "rev-parse", "HEAD") == candidate["commit"]:
            return "current", None
        return "available", candidate

    # Without a Git origin, the checkout's own remote is the upstream.
    if not app.is_dir():
        raise ConfigValidationError(
            "Git update check cannot inspect a missing App without a Git [origin]"
        )
    local = _git("-C", str(app), "rev-parse", "HEAD")
    url = _git("-C", str(app), "remote", "get-url", check["remote"])
    remote = _git("ls-remote", "--exit-code", url, check["ref"]).split()[0]
    if local == remote:
        return "current", None
    return "available", _git_candidate(identity, state, url, check["ref"], remote)


def _git_candidate(
    identity: PackageIdentity, state: Dict[str, Any], url: str, ref: str, commit: str
) -> Dict[str, Any]:
    """Build a Git candidate whose version is a stable assigned timestamp."""
    candidate_id = f"git:{commit}"
    return {
        "candidateId": candidate_id,
        "version": _candidate_version(state, candidate_id, identity.package_root),
        "url": url,
        "ref": ref,
        "commit": commit,
    }


def _git(*arguments: str) -> str:
    """Run one Git command and return its stripped standard output."""
    return subprocess.run(
        ["git", *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def _candidate_version(state: Dict[str, Any], candidate_id: str, package_root: Path) -> str:
    """Reuse a candidate's assigned version or reserve a unique UTC name.

    Git commits have no release version, so each commit is assigned a
    ``YYYYMMDD-HHMMSS-git`` name once and keeps it across later checks. A new
    name never collides with an earlier assignment or an existing directory.
    """
    assignments = state.setdefault("assignedVersion", [])
    for assigned in assignments:
        if assigned.get("candidateId") == candidate_id:
            return assigned["version"]
    taken = {assigned.get("version") for assigned in assignments if isinstance(assigned, dict)}
    timestamp = datetime.now(timezone.utc).replace(microsecond=0)
    while True:
        version = timestamp.strftime("%Y%m%d-%H%M%S-git")
        if version not in taken and not (package_root / f"v{version}").exists():
            break
        timestamp += timedelta(seconds=1)
    assignments.append({"candidateId": candidate_id, "version": version})
    return version


def _normalize_update_candidate(
    raw: Any, identity: PackageIdentity, state: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a discovered update before it is allowed to reach staging."""
    if not isinstance(raw, dict):
        raise ConfigValidationError("Update check must return a mapping or None")
    required = {"candidateId", "version", "url"}
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
        raise ConfigValidationError("Update candidate ID and version must be non-empty strings")
    if (
        "/" in version
        or "\\" in version
        or ".." in version
        or not is_version_directory_name(f"v{version}")
    ):
        raise ConfigValidationError("Update candidate version is unsafe for a version directory")

    # Bootstrap templates have no real version; every other package may only
    # move forward, and the active version may be republished only as a repair
    # of a missing payload or by the same candidate.
    if not identity.version.startswith("bootstrap"):
        comparison = compare_package_versions(version, identity.version)
        if comparison < 0:
            raise ConfigValidationError("Update candidate is older than the active version")
        if (
            comparison == 0
            and app_has_payload(identity)
            and candidate_id != state.get("lastCandidateId")
        ):
            raise ConfigValidationError("A different candidate cannot republish the active version")
    return dict(raw)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def next_version_identity(
    identity: PackageIdentity, candidate: Dict[str, Any]
) -> PackageIdentity:
    """Return the candidate's plain immutable version identity.

    Parameters
    ----------
    identity : PackageIdentity
        Any version of the package receiving the update.
    candidate : Dict[str, Any]
        Normalized candidate.

    Returns
    -------
    PackageIdentity
        Identity of ``<package-root>/v<candidate version>``.
    """
    path = identity.package_root / f"v{candidate['version']}"
    return PackageIdentity.from_version_path(identity.package_root, path, is_current=False)


def prepare_update(
    identity: PackageIdentity,
    config: Dict[str, Any],
    candidate: Dict[str, Any],
    work: Path,
    *,
    no_checksum: bool,
    local_deps_autoinstall: bool = False,
) -> Path:
    """Build a complete new version tree under *work* for a single final rename.

    Parameters
    ----------
    identity : PackageIdentity
        Package version whose non-payload files are copied into the new version.
    config : Dict[str, Any]
        Normalized package configuration for payload preparation.
    candidate : Dict[str, Any]
        Validated update candidate to stage.
    work : Path
        Isolated, empty workspace that receives the staged version.
    no_checksum : bool
        Whether applicable payload checksum verification may be bypassed.
    local_deps_autoinstall : bool, default=False
        Whether trusted package-local hooks may install missing imports.

    Returns
    -------
    Path
        The staged version directory, ready to be renamed to
        ``next_version_identity(identity, candidate).version_path``.
    """
    staged_identity = next_version_identity(identity, candidate)
    stage = work / "version"
    stage.mkdir(parents=True)

    # Copy the package's support tree (including empty defaults such as
    # ``config.default``) and regenerate only App from the update payload.
    for source in identity.version_path.iterdir():
        if source.name.casefold() == "app":
            continue
        if source.is_dir():
            shutil.copytree(source, stage / source.name)
        else:
            shutil.copy2(source, stage / source.name)
    manifest = stage / "pkg.toml"
    rendered, _ = sync_config_metadata_text(manifest.read_text(encoding="utf-8"), staged_identity)
    write_text_atomic(manifest, rendered)

    # Build App from the candidate: Git payloads check out the exact commit;
    # every other payload downloads one verified artifact first.
    stage_app = stage / "App"
    payload = config["update"]["payload"]
    artifact: Optional[Path] = None
    if payload["mode"] == "git":
        subprocess.run(["git", "clone", "--no-checkout", candidate["url"], str(stage_app)], check=True)
        subprocess.run(
            ["git", "-C", str(stage_app), "checkout", "--detach", candidate["commit"]], check=True
        )
    else:
        artifact = _download_candidate(candidate, payload, work, no_checksum=no_checksum)
        if payload["mode"] == "zip":
            _unpack_zip_payload(artifact, candidate, payload, work, stage_app)
            _apply_payload_renames(stage_app, payload.get("rename", []), staged_identity)
        else:
            unpack = _hook(identity, payload["module"], "unpack_app", local_deps_autoinstall)
            run_with_missing_dependencies(
                unpack,
                _stage_context(candidate, artifact, stage, stage_app),
                autoinstall=local_deps_autoinstall,
            )

    # Package-authored install steps run only against the staged tree, so a
    # failing step never touches the live version.
    for step in config["update"]["steps"][1:]:
        install_step = _hook(identity, step["module"], "install_step", local_deps_autoinstall)
        run_with_missing_dependencies(
            install_step,
            _stage_context(candidate, artifact, stage, stage_app),
            autoinstall=local_deps_autoinstall,
        )
    if not (stage_app.is_dir() and any(stage_app.iterdir())):
        raise RuntimeError("Prepared update App directory is missing or empty")
    return stage


def _download_candidate(
    candidate: Dict[str, Any], payload: Dict[str, Any], work: Path, *, no_checksum: bool
) -> Path:
    """Download a candidate artifact into *work* and verify its SHA-256 digest."""
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
            or any(character in name + value for character in "\r\n")
            for name, value in headers.items()
        )
    ):
        raise ConfigValidationError("Update candidate headers must be safe strings")

    # Download into disposable work before any checksum decision.
    download = work / "download"
    download.mkdir()
    artifact = download / "payload.part"
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=60) as response:
        download_response(response, artifact, label="Downloading update")

    # A checksum is mandatory unless the invocation or the package explicitly
    # opts out; an opt-out is always reported.
    bypass = "cli-bypass" if no_checksum else ("version-ignore" if payload["ignore_checksum"] else None)
    if bypass:
        log_warning(f"Checksum verification bypassed for {candidate['version']} ({bypass})")
        return artifact
    checksum = candidate.get("sha256")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-fA-F]{64}", checksum) is None:
        raise ConfigValidationError("Update candidate requires a sha256 checksum")
    if file_sha256(artifact) != checksum.lower():
        raise RuntimeError("Update checksum did not match downloaded file")
    return artifact


def _unpack_zip_payload(
    artifact: Path,
    candidate: Dict[str, Any],
    payload: Dict[str, Any],
    work: Path,
    stage_app: Path,
) -> None:
    """Populate the staged App from a ZIP archive or a single release executable."""
    file_name = candidate.get("fileName")
    if not isinstance(file_name, str):
        file_name = Path(urllib.parse.unquote(urllib.parse.urlparse(candidate["url"]).path)).name

    # A release executable is already the complete application payload; keep
    # its published name inside the staged App.
    if file_name.lower().endswith(".exe"):
        if Path(file_name).name != file_name or PureWindowsPath(file_name).name != file_name:
            raise ConfigValidationError("Update executable fileName must be a name")
        if payload.get("extract") or payload.get("extractSubdir"):
            raise ConfigValidationError(
                "Update executable payloads cannot use ZIP extraction settings"
            )
        stage_app.mkdir()
        shutil.copy2(artifact, stage_app / file_name)
        return

    # Archives are extracted safely, then either mapped piecewise into App or
    # copied from one selected subdirectory (falling back to the archive root).
    extract = work / "extract"
    extract.mkdir()
    safe_extract_zip(artifact, extract)
    if payload.get("extract"):
        copy_zip_extract_mappings(extract, stage_app, payload["extract"])
        return
    source = extract / candidate.get("extractSubdir", payload.get("extractSubdir", ""))
    if not source.exists():
        source = extract
    if not source.is_dir() or not source.resolve().is_relative_to(extract.resolve()):
        raise RuntimeError("Update extractSubdir was not found safely")
    stage_app.mkdir()
    copy_directory_contents(source, stage_app)


def _apply_payload_renames(
    app_path: Path, mappings: list[Dict[str, str]], identity: PackageIdentity
) -> None:
    """Rename configured files or directories within a staged ``App`` tree."""
    resolved_app = app_path.resolve()
    for mapping in mappings:
        # Expand the release version only after the candidate identity is
        # known, then check containment before every filesystem mutation.
        source = app_path / expand_text(mapping["src"], identity, ExpansionMode.GENERAL).value
        destination = app_path / expand_text(mapping["dest"], identity, ExpansionMode.GENERAL).value
        resolved_source = source.resolve()
        resolved_destination = destination.resolve(strict=False)
        if (
            not resolved_source.is_relative_to(resolved_app)
            or not resolved_destination.is_relative_to(resolved_app)
            or resolved_app in {resolved_source, resolved_destination}
        ):
            raise RuntimeError("Update rename path escapes App")
        if not source.exists():
            raise RuntimeError(f"Update rename source was not found: {mapping['src']!r}")
        if destination.exists():
            raise RuntimeError(f"Update rename destination already exists: {mapping['dest']!r}")
        if source.is_dir() and resolved_destination.is_relative_to(resolved_source):
            raise RuntimeError("Update rename destination cannot be inside its source")

        # Intentional subdirectory renames may create parents, but a rename
        # never replaces an existing staged file or directory.
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)


def _stage_context(
    candidate: Dict[str, Any], artifact: Optional[Path], stage: Path, stage_app: Path
) -> Dict[str, Any]:
    """Build the context passed to unpack and install-step hooks."""
    return {
        "apiVersion": 1,
        "candidate": dict(candidate),
        "paths": {
            "artifact": artifact,
            "stageRoot": stage,
            "stageApp": stage_app,
            "stagePayload": stage_app,
        },
    }


# ---------------------------------------------------------------------------
# Package-local hooks
# ---------------------------------------------------------------------------


def load_package_module(identity: PackageIdentity, reference: str):
    """Load one trusted package-local module without retaining its namespace.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that owns the ``pkg.local`` tree.
    reference : str
        Version-relative ``pkg.local/...`` module path.

    Returns
    -------
    module
        The executed module, which must declare ``PKG_MODULE_API = 1``.

    Raises
    ------
    ConfigValidationError
        If the module is missing, cannot be loaded, or declares another API.
    """
    path = (identity.version_path / reference).resolve()
    local_root = (identity.version_path / "pkg.local").resolve()
    if not path.exists():
        raise ConfigValidationError(f"Package-local module does not exist: {reference}")

    # A unique name per file revision keeps unrelated packages' hooks, and
    # edited hooks, from sharing import state.
    name = "_gupkg_local_" + hashlib.sha256(
        (str(path) + str(path.stat().st_mtime_ns)).encode()
    ).hexdigest()[:16]
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
        # Remove this module family only; package-local relative imports use
        # the same unique prefix and must not leak into later update actions.
        for imported_name in list(sys.modules):
            if imported_name == name or imported_name.startswith(name + "."):
                sys.modules.pop(imported_name, None)
    if getattr(module, "PKG_MODULE_API", None) != 1:
        raise ConfigValidationError(f"{reference} must declare PKG_MODULE_API = 1")
    return module


def _hook(identity: PackageIdentity, reference: str, function: str, autoinstall: bool):
    """Load a package-local module and return its required hook function."""
    module = run_with_missing_dependencies(
        load_package_module, identity, reference, autoinstall=autoinstall
    )
    callback = getattr(module, function, None)
    if not callable(callback):
        raise ConfigValidationError(f"{reference} must define {function}(context)")
    return callback
