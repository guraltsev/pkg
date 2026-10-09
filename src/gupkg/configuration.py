"""Normalize and validate canonical ``pkg.toml`` configuration.

Configuration is represented as dictionaries and lists close to the documented
TOML schema. Strict validation produces one runtime shape while directory-derived
identity remains authoritative for package-owned metadata.

Usage and API
-------------
Call ``read_runtime_config(...)`` for validated runtime data and raw metadata
used by higher-level install and health-check workflows.
``normalize_runtime_config(...)`` and ``validate_runtime_config(...)`` validate
an already parsed document, and ``check_metadata_consistency(...)`` compares
directory-owned fields.

Implementation Approach
-----------------------
Strict key and value normalization produces one canonical runtime shape close
to the TOML schema. Every table rejects unknown keys and points legacy
spellings at their canonical replacement. Metadata checks compare
directory-owned values without rewriting the caller's parsed source document.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import (
    ConfigValidationError,
    PackageIdentity,
    read_toml_file,
)


# Canonical top-level ``pkg.toml`` keys, in documentation order.
TOP_LEVEL_KEYS = (
    "name",
    "version",
    "localVersion",
    "description",
    "homepage",
    "origin",
    "update",
    "only_portable",
    "environment",
    "shortcut",
    "path",
    "bin",
)

# Lower-cased legacy top-level spellings mapped to their canonical key, or to
# ``None`` when the legacy construct has no direct replacement.
LEGACY_TOP_LEVEL_HINTS: Dict[str, Optional[str]] = {
    "env": "environment",
    "shortcuts": "shortcut",
    "portable": "only_portable",
    "onlyportable": "only_portable",
    "local_version": "localVersion",
    "downloadurl": "origin",
    "download_url": "origin",
    "main": None,
}


def read_runtime_config(
    identity: PackageIdentity,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Read and validate ``pkg.toml`` for one package version.

    Parameters
    ----------
    identity : PackageIdentity
        Package identity whose ``pkg.toml`` should be loaded.

    Returns
    -------
    Tuple[Dict[str, Any], Dict[str, Any], List[str]]
        A tuple ``(runtime_config, raw_dict, warnings)`` where ``raw_dict`` is
        the file-authored config when ``pkg.toml`` exists, or ``{}`` when the
        package has no configuration file and defaults apply.

    Raises
    ------
    ConfigValidationError
        If the config cannot be read, is not valid TOML, or is structurally
        invalid.

    """
    toml_path = identity.version_path / "pkg.toml"

    # A missing file is a supported, defaults-only package definition.
    if not toml_path.exists():
        config = normalize_runtime_config({}, identity)
        return config, {}, [
            f"No pkg.toml found at {toml_path}; using defaults without creating a file."
        ]

    # An unreadable or malformed file is a configuration problem the package
    # author must fix, so it is reported like any other validation failure.
    try:
        loaded = read_toml_file(toml_path)
    except ConfigValidationError:
        raise
    except Exception as exc:
        raise ConfigValidationError(f"Error loading TOML config from {toml_path}: {exc}") from exc
    config = normalize_runtime_config(loaded, identity)
    validate_runtime_config(config)
    return config, dict(loaded), []


def normalize_runtime_config(raw: Any, identity: PackageIdentity) -> Dict[str, Any]:
    """Normalize raw config data into one canonical runtime mapping.

    Parameters
    ----------
    raw : Any
        Parsed TOML data or ``None``.
    identity : PackageIdentity
        Directory-derived package identity used to supply defaults.

    Returns
    -------
    Dict[str, Any]
        A normalized dictionary that stays close to the canonical ``pkg.toml``
        shape. The install path uses this one representation directly.

    Raises
    ------
    ConfigValidationError
        If *raw* is not a canonical configuration table.

    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"Configuration must be a TOML table, got: {type(raw).__name__}"
        )
    validate_top_level_keys(raw)

    # Directory-owned metadata is type-checked here; its agreement with the
    # directory layout is reported separately by check_metadata_consistency.
    _optional_string(raw.get("name"), field_name="name")
    _optional_string(raw.get("version"), field_name="version")
    if raw.get("localVersion") is not None:
        _local_version(raw["localVersion"])
    only_portable = raw.get("only_portable")
    if only_portable is None:
        only_portable = identity.only_portable_by_name
    elif not isinstance(only_portable, bool):
        raise ConfigValidationError(
            f"'only_portable' must be a boolean, got: {type(only_portable).__name__}"
        )

    # Origin module references must stay beneath pkg.local for every source,
    # including historical entries that are not currently selected.
    origin = normalize_origin_config(raw.get("origin"), identity.version)
    if origin is not None:
        for source in (origin, *origin.get("versions", [])):
            if source.get("mode") == "module":
                source["module"] = _package_local_module(
                    identity, source["module"], context="origin"
                )

    # A Git origin and Git update check describe one repository and ref.
    update = normalize_update_config(raw.get("update"), identity, origin)
    if (
        origin is not None
        and origin.get("mode") == "git"
        and update is not None
        and update["check"]["mode"] == "git"
        and origin["ref"] != update["check"]["ref"]
    ):
        raise ConfigValidationError("Git origin and update check must use the same ref")

    return {
        "description": _optional_string(raw.get("description"), field_name="description"),
        "homepage": _optional_string(raw.get("homepage"), field_name="homepage"),
        "origin": origin,
        "update": update,
        "only_portable": only_portable,
        "environment": normalize_environment_entries(raw.get("environment")),
        "shortcut": normalize_shortcut_entries(raw.get("shortcut")),
        "path": normalize_path_entries(raw.get("path")),
        "bin": normalize_bin_entries(raw.get("bin")),
    }


def validate_runtime_config(config: Dict[str, Any]) -> None:
    """Validate required fields in a normalized runtime config.

    Parameters
    ----------
    config : Dict[str, Any]
        Runtime config to validate.

    Raises
    ------
    ConfigValidationError
        If required fields are missing; every missing field is reported.

    """
    errors: List[str] = []

    def require(kind: str, index: int, missing: List[str]) -> None:
        """Record one row's missing keys when there are any."""
        if missing:
            errors.append(f"{kind}[{index}] missing required key(s): {', '.join(missing)}")

    for index, shortcut in enumerate(config["shortcut"]):
        require("shortcut", index, [
            key for key in ("name", "targetPath") if not shortcut[key].strip()
        ])
    for index, env in enumerate(config["environment"]):
        missing = ["Name"] if not env["Name"].strip() else []
        if env["Value"] == "":
            missing.append("Value")
        require("environment", index, missing)
    for index, wrapper in enumerate(config["bin"]):
        missing = ["name"] if not wrapper["name"].strip() else []
        if not wrapper.get("content") and not wrapper.get("target"):
            missing.append("target or content")
        require("bin", index, missing)
    if errors:
        joined = "\n  - " + "\n  - ".join(errors)
        raise ConfigValidationError(f"Invalid configuration:{joined}")


def check_metadata_consistency(
    identity: PackageIdentity, raw_config: Dict[str, Any]
) -> List[str]:
    """Compare directory-derived metadata with raw configuration metadata.

    Parameters
    ----------
    identity : PackageIdentity
        Package identity derived from the directory layout.
    raw_config : Dict[str, Any]
        Raw config dictionary derived from ``pkg.toml``.

    Returns
    -------
    List[str]
        Human-readable mismatch descriptions; empty when consistent. Absent or
        empty metadata fields are not mismatches.

    Raises
    ------
    TypeError
        If *raw_config* is not a dictionary.

    """
    if not isinstance(raw_config, dict):
        raise TypeError("raw_config must be a dict")

    inconsistencies: List[str] = []

    # The directory owns the canonical name; case-only differences identify
    # the same package and config-fix writes the directory spelling back.
    name = _optional_string(raw_config.get("name"), field_name="name")
    if name and name.casefold() != identity.name.casefold():
        inconsistencies.append(
            f"Name mismatch: directory='{identity.name}', config='{name}'"
        )
    version = _optional_string(raw_config.get("version"), field_name="version")
    if version and version != identity.version:
        inconsistencies.append(
            f"Version mismatch: directory='{identity.version}', config='{version}'"
        )
    local_version = raw_config.get("localVersion")
    if local_version not in (None, "") and _local_version(local_version) != identity.local_version:
        inconsistencies.append(
            f"LocalVersion mismatch: directory='{identity.local_version}', config='{local_version}'"
        )
    only_portable = raw_config.get("only_portable")
    if only_portable is not None:
        if not isinstance(only_portable, bool):
            raise ConfigValidationError(
                f"'only_portable' must be a boolean, got: {type(only_portable).__name__}"
            )
        if only_portable != identity.only_portable_by_name:
            inconsistencies.append(
                f"Portable flag mismatch: directory='{identity.only_portable_by_name}', config='{only_portable}'"
            )
    return inconsistencies


def validate_top_level_keys(raw: Dict[str, Any]) -> None:
    """Reject unknown and legacy top-level keys in one parsed ``pkg.toml``.

    Parameters
    ----------
    raw : Dict[str, Any]
        Parsed top-level TOML table.

    Raises
    ------
    ConfigValidationError
        If *raw* contains an unknown or legacy key.
    """
    _validate_exact_keys(raw, TOP_LEVEL_KEYS, "config", legacy_hints=LEGACY_TOP_LEVEL_HINTS)


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------

# Keys accepted by one origin source table, current or historical.
_ORIGIN_SOURCE_KEYS = (
    "mode",
    "url",
    "ref",
    "version",
    "checksum",
    "extractSubdir",
    "script",
    "module",
)


def normalize_origin_config(
    raw_origin: Any, package_version: str
) -> Optional[Dict[str, Any]]:
    """Normalize the optional ``[origin]`` table.

    Parameters
    ----------
    raw_origin : Any
        Parsed ``origin`` value.
    package_version : str
        Directory-derived upstream version, used to select a historical entry
        when the table has no inline source.

    Returns
    -------
    dict or None
        The active normalized source with an optional ``versions`` history, or
        ``None`` when no origin is declared.

    Raises
    ------
    ConfigValidationError
        If the table or any of its sources is invalid.
    """
    if raw_origin is None:
        return None
    if not isinstance(raw_origin, dict):
        raise ConfigValidationError(
            f"'origin' must be a table, got: {type(raw_origin).__name__}"
        )
    inline_keys = tuple(key for key in _ORIGIN_SOURCE_KEYS if key != "version")
    _validate_exact_keys(raw_origin, (*inline_keys, "versions"), "origin")

    # Historical origins share the provider fields of the current origin but
    # must always name a unique version.
    versions: List[Dict[str, str]] = []
    raw_versions = raw_origin.get("versions")
    if raw_versions is not None:
        if not isinstance(raw_versions, list):
            raise ConfigValidationError(
                f"'origin.versions' must be a list, got: {type(raw_versions).__name__}"
            )
        seen: set[str] = set()
        for index, item in enumerate(raw_versions):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'origin.versions[{index}]' must be a table, got: {type(item).__name__}"
                )
            entry = normalize_origin_history_source(item, context=f"origin.versions[{index}]")
            if entry["version"] in seen:
                raise ConfigValidationError(
                    f"[origin.versions] contains duplicate version: {entry['version']}"
                )
            seen.add(entry["version"])
            versions.append(entry)

    # An inline source is authoritative; otherwise the history entry matching
    # the package version becomes the active source.
    if any(raw_origin.get(key) is not None for key in ("url", "script", "module")):
        current_source = {key: raw_origin[key] for key in inline_keys if key in raw_origin}
        normalized = normalize_origin_source(current_source, context="origin", require_version=False)
    elif versions:
        normalized = next(
            (dict(item) for item in versions if item["version"] == package_version), None
        )
        if normalized is None:
            raise ConfigValidationError(
                "[[origin.versions]] must contain an entry matching top-level version"
            )
    else:
        raise ConfigValidationError(
            "[origin] must declare exactly one of 'url', 'script', or 'module'"
        )
    if versions:
        normalized["versions"] = versions
    return normalized


def normalize_origin_source(
    raw_source: Dict[str, Any], *, context: str, require_version: bool
) -> Dict[str, str]:
    """Normalize one origin source table into a Git, ZIP, script, or module source.

    Parameters
    ----------
    raw_source : Dict[str, Any]
        Parsed source table.
    context : str
        Table location used in error messages.
    require_version : bool
        Whether the source must name a ``version``.

    Returns
    -------
    Dict[str, str]
        Normalized source with an explicit ``mode``.

    Raises
    ------
    ConfigValidationError
        If the source is ambiguous, unsafe, or incomplete.
    """
    _validate_exact_keys(raw_source, _ORIGIN_SOURCE_KEYS, context)
    values = {
        key: _optional_string(raw_source.get(key), field_name=f"{context}.{key}")
        for key in _ORIGIN_SOURCE_KEYS
    }
    mode, url, script, module = values["mode"], values["url"], values["script"], values["module"]
    if require_version and not values["version"]:
        raise ConfigValidationError(f"[{context}].version is required")
    for key in ("url", "script", "module"):
        if values[key] is not None and not values[key].strip():
            raise ConfigValidationError(f"[{context}].{key} must not be empty")

    # Git sources pin a full ref and never use archive-only options.
    if mode == "git":
        if not url or script is not None or module is not None:
            raise ConfigValidationError(
                f"[{context}] Git origin requires 'url' and cannot declare 'script' or 'module'"
            )
        if url.startswith("-") or any(ord(character) < 32 for character in url):
            raise ConfigValidationError(f"[{context}].url is not a safe Git URL")
        if values["checksum"] is not None or values["extractSubdir"] is not None:
            raise ConfigValidationError(
                f"[{context}] Git origin cannot declare checksum or extractSubdir"
            )
        ref = values["ref"] or "refs/heads/main"
        if not ref.startswith("refs/"):
            raise ConfigValidationError(
                f"[{context}].ref must be a full refs/... string for Git origin"
            )
        normalized = {"mode": "git", "url": url, "ref": ref}
    else:
        # Every other source is inferred from exactly one provider field.
        if mode not in {None, "module"}:
            raise ConfigValidationError(f"[{context}].mode must be 'git' or 'module' when provided")
        if values["ref"] is not None:
            raise ConfigValidationError(f"[{context}].ref is supported only when mode = 'git'")
        if sum(value is not None for value in (url, script, module)) != 1:
            raise ConfigValidationError(
                f"[{context}] must declare exactly one of 'url', 'script', or 'module'"
            )
        if mode == "module" and module is None:
            raise ConfigValidationError(f"[{context}] module origin requires 'module'")
        if module is not None:
            normalized = {"mode": "module", "module": module}
        elif script is not None:
            normalized = {"mode": "script", "script": script}
        else:
            normalized = {"mode": "zip", "url": _http_url(url, context=context)}
            if values["checksum"]:
                normalized["checksum"] = _sha256_checksum(values["checksum"], context=context)
            if values["extractSubdir"] is not None:
                normalized["extractSubdir"] = values["extractSubdir"]
    if values["version"] is not None:
        normalized["version"] = values["version"]
    return normalized


def normalize_origin_history_source(
    raw_source: Dict[str, Any], *, context: str
) -> Dict[str, str]:
    """Normalize one ``[[origin.versions]]`` entry.

    A historical entry may record only its version; when it declares a
    provider it follows the same rules as the current origin.

    Parameters
    ----------
    raw_source : Dict[str, Any]
        Parsed history table.
    context : str
        Table location used in error messages.

    Returns
    -------
    Dict[str, str]
        Normalized entry that always contains ``version``.

    Raises
    ------
    ConfigValidationError
        If the entry is invalid or has no version.
    """
    _validate_exact_keys(raw_source, _ORIGIN_SOURCE_KEYS, context)
    if any(raw_source.get(key) is not None for key in ("mode", "url", "script", "module")):
        return normalize_origin_source(raw_source, context=context, require_version=True)

    # A version-only placeholder may still record archive metadata for later.
    version = _optional_string(raw_source.get("version"), field_name=f"{context}.version")
    if not version:
        raise ConfigValidationError(f"[{context}].version is required")
    if raw_source.get("ref") is not None:
        raise ConfigValidationError(f"[{context}].ref is supported only when mode = 'git'")
    normalized: Dict[str, str] = {"version": version}
    checksum = _optional_string(raw_source.get("checksum"), field_name=f"{context}.checksum")
    if checksum:
        normalized["checksum"] = _sha256_checksum(checksum, context=context)
    extract_subdir = _optional_string(
        raw_source.get("extractSubdir"), field_name=f"{context}.extractSubdir"
    )
    if extract_subdir is not None:
        normalized["extractSubdir"] = extract_subdir
    return normalized


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def normalize_update_config(
    raw_update: Any,
    identity: PackageIdentity,
    origin: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize the optional update check, payload, and install-step tables.

    Parameters
    ----------
    raw_update : Any
        Parsed ``update`` value.
    identity : PackageIdentity
        Package version used to validate package-local module paths.
    origin : dict, optional
        Normalized origin, which supplies Git defaults and GitHub URLs.

    Returns
    -------
    dict or None
        ``{"check", "payload", "steps"}`` or ``None`` when updates are not
        configured.

    Raises
    ------
    ConfigValidationError
        If any update table is invalid.
    """
    if raw_update is None:
        return None
    if not isinstance(raw_update, dict):
        raise ConfigValidationError("'update' must be a table")
    _validate_exact_keys(raw_update, ("check", "payload", "steps"), "update")
    payload = raw_update.get("payload")
    if not isinstance(payload, dict):
        raise ConfigValidationError("[update.payload] is a required table")
    git_origin = origin if origin is not None and origin.get("mode") == "git" else None
    check = raw_update.get("check")
    if check is None and git_origin is not None:
        check = {"mode": "git"}
    elif not isinstance(check, dict):
        raise ConfigValidationError("[update.check] is required unless [origin].mode = 'git'")
    normalized_check = _normalize_update_check(check, identity, origin, git_origin)
    normalized_payload = _normalize_update_payload(payload, identity, normalized_check["mode"])

    # The built-in payload step is always first; package-local modules may
    # follow it to adjust the staged tree before it is committed.
    raw_steps = raw_update.get("steps")
    steps: List[Dict[str, str]] = [{"mode": "payload"}]
    if raw_steps is not None:
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ConfigValidationError("[[update.steps]] must contain at least one step")
        for index, step in enumerate(raw_steps):
            context = f"update.steps[{index}]"
            if not isinstance(step, dict):
                raise ConfigValidationError(f"[[{context}]] must be a table")
            if index == 0:
                _validate_exact_keys(step, ("mode",), context)
                if step.get("mode") != "payload":
                    raise ConfigValidationError(
                        "The first [[update.steps]] entry must use mode = 'payload'"
                    )
                continue
            _validate_exact_keys(step, ("mode", "module"), context)
            if step.get("mode") != "module":
                raise ConfigValidationError(
                    f"[[{context}]].mode must be 'module' after the payload step"
                )
            if not isinstance(step.get("module"), str):
                raise ConfigValidationError(f"[[{context}]].module must be a string")
            steps.append({
                "mode": "module",
                "module": _package_local_module(identity, step["module"], context=context),
            })
    return {"check": normalized_check, "payload": normalized_payload, "steps": steps}


def _normalize_update_check(
    check: Dict[str, Any],
    identity: PackageIdentity,
    origin: Optional[Dict[str, Any]],
    git_origin: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalize ``[update.check]`` for one of the Git, GitHub, or module modes."""
    mode = check.get("mode")
    if mode == "git":
        _validate_exact_keys(check, ("mode", "appPath", "remote", "ref"), "update.check")
        app_path = check.get("appPath", "App")
        ref = check.get("ref", git_origin["ref"] if git_origin else "refs/heads/main")
        remote = check.get("remote", "origin")
        if not _is_safe_relative(app_path):
            raise ConfigValidationError("[update.check].appPath must be a safe relative path")
        if not isinstance(ref, str) or not ref.startswith("refs/"):
            raise ConfigValidationError("[update.check].ref must be a full refs/... string")
        if not isinstance(remote, str) or not remote or remote.startswith("-"):
            raise ConfigValidationError("[update.check].remote must be a Git remote name")
        return {"mode": "git", "appPath": app_path, "remote": remote, "ref": ref}

    if mode == "github":
        _validate_exact_keys(check, ("mode", "assetName", "tagPrefix"), "update.check")
        if origin is None or not isinstance(origin.get("url"), str):
            raise ConfigValidationError("GitHub update checks require [origin].url")
        asset_name = check.get("assetName")
        if not isinstance(asset_name, str) or not asset_name:
            raise ConfigValidationError("[update.check].assetName must be a non-empty string")
        normalized = {"mode": "github", "url": origin["url"], "assetName": asset_name}
        tag_prefix = check.get("tagPrefix")
        if tag_prefix is not None:
            if not isinstance(tag_prefix, str) or not tag_prefix:
                raise ConfigValidationError(
                    "[update.check].tagPrefix must be a non-empty string when provided"
                )
            normalized["tagPrefix"] = tag_prefix
        return normalized

    if mode == "module":
        _validate_exact_keys(check, ("mode", "module", "channel"), "update.check")
        module = check.get("module", "pkg.local/check_update.py")
        channel = check.get("channel", "stable")
        if not isinstance(module, str):
            raise ConfigValidationError("[update.check].module must be a string")
        if not isinstance(channel, str) or not channel:
            raise ConfigValidationError("[update.check].channel must be a non-empty string")
        return {
            "mode": "module",
            "module": _package_local_module(identity, module, context="update.check"),
            "channel": channel,
        }

    raise ConfigValidationError("[update.check].mode must be 'git', 'github', or 'module'")


def _normalize_update_payload(
    payload: Dict[str, Any], identity: PackageIdentity, check_mode: str
) -> Dict[str, Any]:
    """Normalize ``[update.payload]`` and its ZIP extraction and rename maps."""
    mode = payload.get("mode")
    if mode not in {"git", "zip", "module"}:
        raise ConfigValidationError("[update.payload].mode must be 'git', 'zip', or 'module'")
    _validate_exact_keys(
        payload,
        ("mode", "extractSubdir", "extract", "rename", "ignore_checksum", "module", "maxSizeMB"),
        "update.payload",
    )
    if mode == "git" and check_mode != "git":
        raise ConfigValidationError(
            f"[update.payload].mode = '{mode}' requires a git update check"
        )
    ignore_checksum = payload.get("ignore_checksum", False)
    if not isinstance(ignore_checksum, bool):
        raise ConfigValidationError("[update.payload].ignore_checksum must be a boolean")
    normalized: Dict[str, Any] = {"mode": mode, "ignore_checksum": ignore_checksum}

    extract_subdir = payload.get("extractSubdir")
    if extract_subdir is not None:
        if not _is_safe_relative(extract_subdir):
            raise ConfigValidationError(
                "[update.payload].extractSubdir must be a safe relative path"
            )
        normalized["extractSubdir"] = extract_subdir

    # Extraction maps compose App from ZIP-root wildcards; their destinations
    # are literal App-relative paths.
    if payload.get("extract") is not None:
        if extract_subdir is not None:
            raise ConfigValidationError(
                "[update.payload].extract cannot be combined with extractSubdir"
            )
        normalized["extract"] = _zip_mappings(
            payload["extract"], mode, "extract", src_glob=True, dest_may_be_empty=True
        )

    # Renames move exact paths inside the staged App after extraction.
    if payload.get("rename") is not None:
        normalized["rename"] = _zip_mappings(
            payload["rename"], mode, "rename", src_glob=False, dest_may_be_empty=False
        )

    if mode == "module":
        module = payload.get("module", "pkg.local/unpack_app.py")
        if not isinstance(module, str):
            raise ConfigValidationError("[update.payload].module must be a string")
        normalized["module"] = _package_local_module(identity, module, context="update.payload")
    return normalized


def _zip_mappings(
    raw: Any,
    payload_mode: str,
    name: str,
    *,
    src_glob: bool,
    dest_may_be_empty: bool,
) -> List[Dict[str, str]]:
    """Validate ``[[update.payload.extract]]`` or ``[[update.payload.rename]]`` rows."""
    if payload_mode != "zip":
        raise ConfigValidationError(f"[update.payload].{name} is only supported with mode = 'zip'")
    if not isinstance(raw, list) or not raw:
        raise ConfigValidationError(f"[update.payload].{name} must be a non-empty array of tables")
    mappings: List[Dict[str, str]] = []
    for index, mapping in enumerate(raw):
        context = f"update.payload.{name}[{index}]"
        if not isinstance(mapping, dict):
            raise ConfigValidationError(f"[{context}] must be a table")
        _validate_exact_keys(mapping, ("src", "dest"), context)
        src, dest = mapping.get("src"), mapping.get("dest")
        if not isinstance(src, str) or not src:
            raise ConfigValidationError(f"[{context}].src must be a non-empty string")
        if not isinstance(dest, str) or (not dest and not dest_may_be_empty):
            raise ConfigValidationError(f"[{context}].dest must be a non-empty string")

        # Every path stays relative to its staging root; only an extraction
        # source may use shell wildcards to match versioned archive names.
        for field_name, value, allow_glob in (("src", src, src_glob), ("dest", dest, False)):
            path = PureWindowsPath(value)
            if (
                path.is_absolute()
                or path.drive
                or ".." in path.parts
                or (not dest_may_be_empty and value in {".", ".."})
                or (not allow_glob and any(character in value for character in "*?[]"))
            ):
                root = "the zip root" if allow_glob else "$App"
                raise ConfigValidationError(
                    f"[{context}].{field_name} must be a safe path relative to {root}"
                )
        mappings.append({"src": src, "dest": dest})
    return mappings


# ---------------------------------------------------------------------------
# Component rows
# ---------------------------------------------------------------------------


def normalize_environment_entries(raw_environment: Any) -> List[Dict[str, str]]:
    """Normalize canonical ``[[environment]]`` rows into ``Name``/``Value`` maps.

    Parameters
    ----------
    raw_environment : Any
        Parsed value of the ``environment`` configuration key.

    Returns
    -------
    List[Dict[str, str]]
        Canonical environment rows; missing strings become ``""``.

    Raises
    ------
    ConfigValidationError
        If the value or one of its rows has an invalid shape.

    """
    rows: List[Dict[str, str]] = []
    for index, item in _table_rows(raw_environment, "environment"):
        context = f"environment[{index}]"
        _validate_exact_keys(
            item, ("Name", "Value"), context, legacy_hints={"name": "Name", "value": "Value"}
        )
        rows.append({
            key: _optional_string(item.get(key), field_name=f"{context}.{key}") or ""
            for key in ("Name", "Value")
        })
    return rows


def normalize_shortcut_entries(raw_shortcut: Any) -> List[Dict[str, str]]:
    """Normalize canonical ``[[shortcut]]`` rows.

    Parameters
    ----------
    raw_shortcut : Any
        Parsed value of the ``shortcut`` configuration key.

    Returns
    -------
    List[Dict[str, str]]
        Canonical shortcut rows with every supported key; missing strings
        become ``""``.

    Raises
    ------
    ConfigValidationError
        If the value or one of its rows has an invalid shape.

    """
    keys = ("name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description")
    hints = {
        "path": "targetPath",
        "target_path": "targetPath",
        "args": "arguments",
        "workdir": "workingDirectory",
        "working_directory": "workingDirectory",
        "icon_location": "iconLocation",
        "desc": "description",
    }
    rows: List[Dict[str, str]] = []
    for index, item in _table_rows(raw_shortcut, "shortcut"):
        context = f"shortcut[{index}]"
        _validate_exact_keys(item, keys, context, legacy_hints=hints)
        rows.append({
            key: _optional_string(item.get(key), field_name=f"{context}.{key}") or ""
            for key in keys
        })
    return rows


def normalize_path_entries(raw_path_entries: Any) -> List[str]:
    """Normalize canonical ``[[path]]`` rows into ordered PATH values.

    Parameters
    ----------
    raw_path_entries : Any
        Parsed value of the ``path`` configuration key.

    Returns
    -------
    List[str]
        Ordered PATH values.

    Raises
    ------
    ConfigValidationError
        If the value or one of its rows has an invalid shape.

    """
    entries: List[str] = []
    for index, item in _table_rows(raw_path_entries, "path"):
        _validate_exact_keys(item, ("value",), f"path[{index}]", legacy_hints={"path": "value"})
        if "value" not in item:
            raise ConfigValidationError(f"'path[{index}]' is missing required key: value")
        value = item["value"]
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"'path[{index}].value' must be a string, got: {type(value).__name__}"
            )
        entries.append(value)
    return entries


def normalize_bin_entries(raw_bin: Any) -> List[Dict[str, Any]]:
    """Normalize ``[[bin]]`` rows into native-shim or raw-content wrappers.

    Parameters
    ----------
    raw_bin : Any
        Parsed value of the ``bin`` configuration key.

    Returns
    -------
    List[Dict[str, Any]]
        Canonical rows. A shim row has ``name``, ``target``, ``type``,
        ``arguments``, ``forward_args``, ``elevate``, and ``working_dir``; a
        content row has only ``name`` and ``content``.

    Raises
    ------
    ConfigValidationError
        If the value or one of its rows has an invalid shape.

    """
    shim_keys = ("target", "type", "arguments", "forward_args", "elevate", "working_dir")
    rows: List[Dict[str, Any]] = []
    for index, item in _table_rows(raw_bin, "bin"):
        context = f"bin[{index}]"
        _validate_exact_keys(item, ("name", *shim_keys, "content"), context)
        name = _optional_string(item.get("name"), field_name=f"{context}.name") or ""

        # Raw content is the shell escape hatch and excludes every shim option.
        # Single-line TOML strings may spell newlines as literal ``\n``.
        if "content" in item:
            if any(key in item for key in shim_keys):
                raise ConfigValidationError(
                    f"'{context}.content' cannot be combined with shim options"
                )
            content = _optional_string(item["content"], field_name=f"{context}.content") or ""
            if "\n" not in content:
                content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
            rows.append({"name": name, "content": content})
            continue

        # Native shims launch their target directly with fixed arguments.
        shim_type = _optional_string(item.get("type"), field_name=f"{context}.type") or "console"
        if shim_type not in {"console", "gui"}:
            raise ConfigValidationError(
                f"'{context}.type' must be 'console' or 'gui', got: {shim_type!r}"
            )
        arguments = item.get("arguments", [])
        if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
            raise ConfigValidationError(f"'{context}.arguments' must be an array of strings")
        flags = {}
        for flag, default in (("forward_args", True), ("elevate", False)):
            flags[flag] = item.get(flag, default)
            if not isinstance(flags[flag], bool):
                raise ConfigValidationError(
                    f"'{context}.{flag}' must be a boolean, got: {type(flags[flag]).__name__}"
                )
        rows.append({
            "name": name,
            "target": _optional_string(item.get("target"), field_name=f"{context}.target") or "",
            "type": shim_type,
            "arguments": list(arguments),
            **flags,
            "working_dir": _optional_string(item.get("working_dir"), field_name=f"{context}.working_dir"),
        })
    return rows


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _validate_exact_keys(
    data: Dict[str, Any],
    allowed: Sequence[str],
    context: str,
    *,
    legacy_hints: Optional[Dict[str, Optional[str]]] = None,
) -> None:
    """Reject keys outside *allowed*, naming the canonical spelling of legacy keys."""
    for key in data:
        if key in allowed:
            continue
        lowered = str(key).lower()
        if legacy_hints is not None and lowered in legacy_hints:
            hint = legacy_hints[lowered]
            if hint is None:
                raise ConfigValidationError(
                    f"Unsupported legacy key '{key}' in {context}. Use canonical top-level metadata keys instead of [[main]]."
                )
            raise ConfigValidationError(
                f"Unsupported legacy key '{key}' in {context}. Use '{hint}' instead."
            )
        raise ConfigValidationError(
            f"Unknown key '{key}' in {context}. Allowed keys: {', '.join(allowed)}"
        )


def _table_rows(raw: Any, name: str) -> List[Tuple[int, Dict[str, Any]]]:
    """Return indexed rows of an optional array of tables, rejecting other shapes."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigValidationError(f"'{name}' must be a list, got: {type(raw).__name__}")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigValidationError(
                f"'{name}[{index}]' must be a table, got: {type(item).__name__}"
            )
    return list(enumerate(raw))


def _optional_string(value: Any, *, field_name: str) -> Optional[str]:
    """Return *value* when it is a string or ``None``, otherwise reject it."""
    if value is not None and not isinstance(value, str):
        raise ConfigValidationError(
            f"'{field_name}' must be a string, got: {type(value).__name__}"
        )
    return value


def _local_version(value: Any) -> int:
    """Normalize ``localVersion``, which may be an integer or a digit string."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ConfigValidationError(
        f"'localVersion' must be an integer, got: {type(value).__name__}"
    )


def _http_url(url: str, *, context: str) -> str:
    """Require an absolute HTTP or HTTPS URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ConfigValidationError(f"[{context}].url must be an HTTP or HTTPS URL")
    return url


def _sha256_checksum(checksum: str, *, context: str) -> str:
    """Normalize ``sha256:<hex>`` checksum text to lower-case hexadecimal."""
    algorithm, separator, expected_hex = checksum.partition(":")
    if separator != ":" or algorithm.lower() != "sha256":
        raise ConfigValidationError(f"[{context}].checksum must use sha256:<hex> syntax")
    if re.fullmatch(r"[0-9A-Fa-f]{64}", expected_hex) is None:
        raise ConfigValidationError(
            f"[{context}].checksum must be sha256 followed by 64 hex characters"
        )
    return f"sha256:{expected_hex.lower()}"


def _is_safe_relative(value: Any) -> bool:
    """Return whether *value* is a relative path string without ``..``."""
    return (
        isinstance(value, str)
        and not Path(value).is_absolute()
        and not PureWindowsPath(value).drive
        and ".." not in Path(value).parts
    )


def _package_local_module(identity: PackageIdentity, value: str, *, context: str) -> str:
    """Validate a Python hook path beneath a version's ``pkg.local`` directory."""
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix.lower() != ".py"
    ):
        raise ConfigValidationError(
            f"[{context}].module must be a relative .py path below pkg.local"
        )
    local_root = (identity.version_path / "pkg.local").resolve()
    resolved = (identity.version_path / candidate).resolve()
    if not resolved.is_relative_to(local_root):
        raise ConfigValidationError(f"[{context}].module must resolve below pkg.local")
    return candidate.as_posix()
