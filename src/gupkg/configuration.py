"""Normalize and validate canonical ``pkg.toml`` configuration.

Configuration is represented as dictionaries and lists close to the documented
TOML schema. Strict validation produces one runtime shape while directory-derived
identity remains authoritative for package-owned metadata.

Usage and API
-------------
Call ``read_runtime_config(...)`` for validated runtime data and raw metadata
used by higher-level install and health-check workflows.

Implementation Approach
-----------------------
Strict key and value normalization produces one canonical runtime shape close
to the TOML schema. Metadata checks compare directory-owned values without
rewriting the caller's parsed source document.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from .core import (
    ConfigValidationError,
    PackageIdentity,
    read_toml_file,
)


def _validate_exact_keys(
    data: Dict[str, Any],
    *,
    allowed: set[str],
    context: str,
    ordered_allowed: List[str],
    legacy_hints: Optional[Dict[str, Optional[str]]] = None,
) -> None:
    """Validate that a mapping uses only canonical keys.

    Parameters
    ----------
    data : Dict[str, Any]
        Mapping to validate.
    allowed : set[str]
        Canonical keys accepted in *context*.
    context : str
        Human-readable location such as ``config`` or ``shortcut[0]``.
    ordered_allowed : List[str]
        Stable ordered list of canonical keys for error text.
    legacy_hints : Optional[Dict[str, Optional[str]]]
        Optional mapping from lower-cased legacy spellings to the
            canonical replacement, or ``None`` for special cases with no direct
            replacement.

    Raises
    ------
    ConfigValidationError
        If *data* contains an unknown or legacy key.

    """
    for key in data.keys():
        if key in allowed:
            continue
        hint = None
        if legacy_hints is not None:
            hint = legacy_hints.get(str(key).lower())
            if str(key).lower() in legacy_hints:
                if hint is None:
                    raise ConfigValidationError(
                        f"Unsupported legacy key '{key}' in {context}. Use canonical top-level metadata keys instead of [[main]]."
                    )
                raise ConfigValidationError(
                    f"Unsupported legacy key '{key}' in {context}. Use '{hint}' instead."
                )
        raise ConfigValidationError(
            f"Unknown key '{key}' in {context}. Allowed keys: {', '.join(ordered_allowed)}"
        )


def _normalize_optional_string(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize an optional string field.

    Parameters
    ----------
    value : Any
        Raw value to normalize.
    field_name : str
        Human-readable field name for error messages.

    Returns
    -------
    Optional[str]
        ``None`` when *value* is ``None``; otherwise the normalized string.

    Raises
    ------
    ConfigValidationError
        If *value* is not a string or ``None``.

    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"'{field_name}' must be a string, got: {type(value).__name__}"
        )
    return value


def _normalize_required_string(value: Any, *, field_name: str) -> str:
    """Normalize a required-or-empty string field.

    Parameters
    ----------
    value : Any
        Raw value to normalize.
    field_name : str
        Human-readable field name for error messages.

    Returns
    -------
    str
        A string value, or ``""`` when *value* is missing.

    Raises
    ------
    ConfigValidationError
        If *value* is present but not a string.

    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"'{field_name}' must be a string, got: {type(value).__name__}"
        )
    return value


def _normalize_local_version_value(value: Any, *, field_name: str) -> int:
    """Normalize the ``localVersion`` scalar.

    Parameters
    ----------
    value : Any
        Raw value to normalize.
    field_name : str
        Human-readable field name for error messages.

    Returns
    -------
    int
        The normalized integer local version.

    Raises
    ------
    ConfigValidationError
        If *value* is not an integer or digit string.

    """
    if isinstance(value, bool):
        raise ConfigValidationError(
            f"'{field_name}' must be an integer, got: {type(value).__name__}"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ConfigValidationError(
        f"'{field_name}' must be an integer, got: {type(value).__name__}"
    )


def _normalize_only_portable_value(value: Any, *, field_name: str) -> bool:
    """Normalize the ``only_portable`` scalar.

    Parameters
    ----------
    value : Any
        Raw value to normalize.
    field_name : str
        Human-readable field name for error messages.

    Returns
    -------
    bool
        The normalized boolean value.

    Raises
    ------
    ConfigValidationError
        If *value* is not a boolean.

    """
    if not isinstance(value, bool):
        raise ConfigValidationError(
            f"'{field_name}' must be a boolean, got: {type(value).__name__}"
        )
    return value


def normalize_origin_source(
    raw_source: Dict[str, Any], *, context: str, require_version: bool
) -> Dict[str, str]:
    """Normalize one origin source table."""
    source_keys = {
        "mode",
        "url",
        "ref",
        "version",
        "checksum",
        "extractSubdir",
        "script",
        "module",
    }
    _validate_exact_keys(
        raw_source,
        allowed=source_keys,
        context=context,
        ordered_allowed=[
            "mode",
            "url",
            "ref",
            "version",
            "checksum",
            "extractSubdir",
            "script",
            "module",
        ],
    )

    mode = _normalize_optional_string(
        raw_source.get("mode"), field_name=f"{context}.mode"
    )
    url = _normalize_optional_string(raw_source.get("url"), field_name=f"{context}.url")
    git_ref = _normalize_optional_string(
        raw_source.get("ref"), field_name=f"{context}.ref"
    )
    origin_version = _normalize_optional_string(
        raw_source.get("version"), field_name=f"{context}.version"
    )
    script = _normalize_optional_string(
        raw_source.get("script"), field_name=f"{context}.script"
    )
    module = _normalize_optional_string(
        raw_source.get("module"), field_name=f"{context}.module"
    )
    checksum = _normalize_optional_string(
        raw_source.get("checksum"), field_name=f"{context}.checksum"
    )
    extract_subdir = _normalize_optional_string(
        raw_source.get("extractSubdir"), field_name=f"{context}.extractSubdir"
    )

    if mode == "git":
        if not url or script is not None or module is not None:
            raise ConfigValidationError(
                f"[{context}] Git origin requires 'url' and cannot declare 'script' or 'module'"
            )
        if url.startswith("-") or any(ord(character) < 32 for character in url):
            raise ConfigValidationError(f"[{context}].url is not a safe Git URL")
        if checksum is not None or extract_subdir is not None:
            raise ConfigValidationError(
                f"[{context}] Git origin cannot declare checksum or extractSubdir"
            )
        git_ref = git_ref or "refs/heads/main"
        if not git_ref.startswith("refs/"):
            raise ConfigValidationError(
                f"[{context}].ref must be a full refs/... string for Git origin"
            )
        if require_version and not origin_version:
            raise ConfigValidationError(f"[{context}].version is required")
        normalized = {"mode": "git", "url": url, "ref": git_ref}
        if origin_version is not None:
            normalized["version"] = origin_version
        return normalized
    if mode not in {None, "module"}:
        raise ConfigValidationError(f"[{context}].mode must be 'git' or 'module' when provided")
    if git_ref is not None:
        raise ConfigValidationError(
            f"[{context}].ref is supported only when mode = 'git'"
        )

    if sum(value is not None for value in (url, script, module)) != 1:
        raise ConfigValidationError(
            f"[{context}] must declare exactly one of 'url', 'script', or 'module'"
        )
    if require_version and not origin_version:
        raise ConfigValidationError(f"[{context}].version is required")

    if module is not None:
        if module.strip() == "":
            raise ConfigValidationError(f"[{context}].module must not be empty")
        normalized = {"mode": "module", "module": module}
        if origin_version is not None:
            normalized["version"] = origin_version
        return normalized

    if mode == "module":
        raise ConfigValidationError(f"[{context}] module origin requires 'module'")

    if url:
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigValidationError(f"[{context}].url must be an HTTP or HTTPS URL")
        normalized: Dict[str, str] = {"mode": "zip", "url": url}
        if origin_version is not None:
            normalized["version"] = origin_version
        if checksum:
            algorithm, separator, expected_hex = checksum.partition(":")
            if separator != ":" or algorithm.lower() != "sha256":
                raise ConfigValidationError(
                    f"[{context}].checksum must use sha256:<hex> syntax"
                )
            if (
                len(expected_hex) != 64
                or re.fullmatch(r"[0-9A-Fa-f]{64}", expected_hex) is None
            ):
                raise ConfigValidationError(
                    f"[{context}].checksum must be sha256 followed by 64 hex characters"
                )
            normalized["checksum"] = f"sha256:{expected_hex.lower()}"
        if extract_subdir is not None:
            normalized["extractSubdir"] = extract_subdir
        return normalized

    if script is not None and script.strip() == "":
        raise ConfigValidationError(f"[{context}].script must not be empty")
    normalized = {"mode": "script", "script": script or ""}
    if origin_version is not None:
        normalized["version"] = origin_version
    return normalized


def normalize_origin_history_source(
    raw_source: Dict[str, Any], *, context: str
) -> Dict[str, str]:
    """Normalize a permissive historical origin source table."""
    source_keys = {
        "mode",
        "url",
        "ref",
        "version",
        "checksum",
        "extractSubdir",
        "script",
        "module",
    }
    _validate_exact_keys(
        raw_source,
        allowed=source_keys,
        context=context,
        ordered_allowed=[
            "mode",
            "url",
            "ref",
            "version",
            "checksum",
            "extractSubdir",
            "script",
            "module",
        ],
    )

    origin_version = _normalize_optional_string(
        raw_source.get("version"), field_name=f"{context}.version"
    )
    if not origin_version:
        raise ConfigValidationError(f"[{context}].version is required")

    if raw_source.get("mode") is not None:
        return normalize_origin_source(
            raw_source, context=context, require_version=True
        )

    normalized: Dict[str, str] = {"version": origin_version}
    url = _normalize_optional_string(raw_source.get("url"), field_name=f"{context}.url")
    script = _normalize_optional_string(
        raw_source.get("script"), field_name=f"{context}.script"
    )
    module = _normalize_optional_string(
        raw_source.get("module"), field_name=f"{context}.module"
    )
    if sum(value is not None for value in (url, script, module)) > 1:
        raise ConfigValidationError(
            f"[{context}] cannot declare more than one of 'url', 'script', or 'module'"
        )
    if url:
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigValidationError(f"[{context}].url must be an HTTP or HTTPS URL")
        normalized["mode"] = "zip"
        normalized["url"] = url
    if script is not None:
        if script.strip() == "":
            raise ConfigValidationError(f"[{context}].script must not be empty")
        normalized["mode"] = "script"
        normalized["script"] = script
    if module is not None:
        if module.strip() == "":
            raise ConfigValidationError(f"[{context}].module must not be empty")
        normalized["mode"] = "module"
        normalized["module"] = module

    checksum = _normalize_optional_string(
        raw_source.get("checksum"), field_name=f"{context}.checksum"
    )
    if checksum:
        algorithm, separator, expected_hex = checksum.partition(":")
        if separator != ":" or algorithm.lower() != "sha256":
            raise ConfigValidationError(
                f"[{context}].checksum must use sha256:<hex> syntax"
            )
        if (
            len(expected_hex) != 64
            or re.fullmatch(r"[0-9A-Fa-f]{64}", expected_hex) is None
        ):
            raise ConfigValidationError(
                f"[{context}].checksum must be sha256 followed by 64 hex characters"
            )
        normalized["checksum"] = f"sha256:{expected_hex.lower()}"

    extract_subdir = _normalize_optional_string(
        raw_source.get("extractSubdir"), field_name=f"{context}.extractSubdir"
    )
    if extract_subdir is not None:
        normalized["extractSubdir"] = extract_subdir
    return normalized


def normalize_origin_config(
    raw_origin: Any, package_version: str
) -> Optional[Dict[str, Any]]:
    """Normalize the optional ``[origin]`` table."""
    if raw_origin is None:
        return None
    if not isinstance(raw_origin, dict):
        raise ConfigValidationError(
            f"'origin' must be a table, got: {type(raw_origin).__name__}"
        )

    origin_keys = {
        "mode",
        "url",
        "ref",
        "checksum",
        "extractSubdir",
        "script",
        "module",
        "versions",
    }
    _validate_exact_keys(
        raw_origin,
        allowed=origin_keys,
        context="origin",
        ordered_allowed=[
            "mode",
            "url",
            "ref",
            "checksum",
            "extractSubdir",
            "script",
            "module",
            "versions",
        ],
    )

    # Historical origins are explicit versioned source entries. They share the
    # same provider fields as the current origin but must always name a version.
    raw_versions = raw_origin.get("versions")
    versions: List[Dict[str, str]] = []
    if raw_versions is not None:
        if not isinstance(raw_versions, list):
            raise ConfigValidationError(
                f"'origin.versions' must be a list, got: {type(raw_versions).__name__}"
            )
        seen_versions: set[str] = set()
        for index, item in enumerate(raw_versions):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'origin.versions[{index}]' must be a table, got: {type(item).__name__}"
                )
            normalized_item = normalize_origin_history_source(
                item, context=f"origin.versions[{index}]"
            )
            item_version = normalized_item["version"]
            if item_version in seen_versions:
                raise ConfigValidationError(
                    f"[origin.versions] contains duplicate version: {item_version}"
                )
            seen_versions.add(item_version)
            versions.append(normalized_item)

    has_inline_source = any(
        bool(raw_origin.get(field)) for field in ("url", "script", "module")
    )
    if has_inline_source:
        current_source = {
            key: raw_origin[key]
            for key in ("mode", "url", "ref", "checksum", "extractSubdir", "script", "module")
            if key in raw_origin
        }
        normalized = normalize_origin_source(
            current_source, context="origin", require_version=False
        )
        if versions:
            normalized["versions"] = versions
        return normalized

    if versions:
        for item in versions:
            if item["version"] == package_version:
                normalized = dict(item)
                normalized["versions"] = versions
                return normalized
        raise ConfigValidationError(
            "[[origin.versions]] must contain an entry matching top-level version"
        )

    raise ConfigValidationError(
        "[origin] must declare exactly one of 'url', 'script', or 'module'"
    )


def _validate_package_local_path(
    identity: PackageIdentity, value: str, *, context: str
) -> str:
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


def normalize_update_config(
    raw_update: Any,
    identity: PackageIdentity,
    origin: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize the optional update check and payload configuration."""
    if raw_update is None:
        return None
    if not isinstance(raw_update, dict):
        raise ConfigValidationError("'update' must be a table")
    _validate_exact_keys(
        raw_update,
        allowed={"check", "payload", "steps"},
        context="update",
        ordered_allowed=["check", "payload", "steps"],
    )
    check = raw_update.get("check")
    payload = raw_update.get("payload")
    if not isinstance(payload, dict):
        raise ConfigValidationError(
            "[update.payload] is a required table"
        )
    if check is None and origin is not None and origin.get("mode") == "git":
        check = {
            "mode": "git",
            "ref": origin["ref"],
        }
    elif not isinstance(check, dict):
        raise ConfigValidationError(
            "[update.check] is required unless [origin].mode = 'git'"
        )
    mode = check.get("mode")
    if mode == "git":
        _validate_exact_keys(
            check,
            allowed={"mode", "appPath", "remote", "ref"},
            context="update.check",
            ordered_allowed=["mode", "appPath", "remote", "ref"],
        )
        app_path = check.get("appPath", "App")
        default_ref = (
            origin["ref"]
            if origin is not None and origin.get("mode") == "git"
            else "refs/heads/main"
        )
        ref = check.get("ref", default_ref)
        if (
            not isinstance(app_path, str)
            or Path(app_path).is_absolute()
            or ".." in Path(app_path).parts
        ):
            raise ConfigValidationError(
                "[update.check].appPath must be a safe relative path"
            )
        if not isinstance(ref, str) or not ref.startswith("refs/"):
            raise ConfigValidationError(
                "[update.check].ref must be a full refs/... string"
            )
        normalized_check = {
            "mode": "git",
            "appPath": app_path,
            "remote": check.get("remote", "origin"),
            "ref": ref,
        }
    elif mode == "github":
        _validate_exact_keys(
            check,
            allowed={"mode", "assetName", "tagPrefix"},
            context="update.check",
            ordered_allowed=["mode", "assetName", "tagPrefix"],
        )
        asset_name = check.get("assetName")
        if origin is None or not isinstance(origin.get("url"), str):
            raise ConfigValidationError(
                "GitHub update checks require [origin].url"
            )
        if not isinstance(asset_name, str) or not asset_name:
            raise ConfigValidationError(
                "[update.check].assetName must be a non-empty string"
            )
        tag_prefix = check.get("tagPrefix")
        if tag_prefix is not None and (
            not isinstance(tag_prefix, str) or not tag_prefix
        ):
            raise ConfigValidationError(
                "[update.check].tagPrefix must be a non-empty string when provided"
            )
        normalized_check = {
            "mode": "github",
            "url": origin["url"],
            "assetName": asset_name,
        }
        if tag_prefix is not None:
            normalized_check["tagPrefix"] = tag_prefix
    elif mode == "module":
        _validate_exact_keys(
            check,
            allowed={"mode", "module", "channel"},
            context="update.check",
            ordered_allowed=["mode", "module", "channel"],
        )
        module = check.get("module", "pkg.local/check_update.py")
        if not isinstance(module, str):
            raise ConfigValidationError("[update.check].module must be a string")
        normalized_check = {
            "mode": "module",
            "module": _validate_package_local_path(
                identity, module, context="update.check"
            ),
            "channel": check.get("channel", "stable"),
        }
    else:
        raise ConfigValidationError(
            "[update.check].mode must be 'git', 'github', or 'module'"
        )
    payload_mode = payload.get("mode")
    if payload_mode not in {"git", "zip", "module"}:
        raise ConfigValidationError(
            "[update.payload].mode must be 'git', 'zip', or 'module'"
        )
    allowed_payload = {
        "mode",
        "extractSubdir",
        "extract",
        "rename",
        "ignore_checksum",
        "module",
        "maxSizeMB",
    }
    _validate_exact_keys(
        payload,
        allowed=allowed_payload,
        context="update.payload",
        ordered_allowed=list(allowed_payload),
    )
    if payload_mode == "git" and mode != "git":
        raise ConfigValidationError(
            f"[update.payload].mode = '{payload_mode}' requires a git update check"
        )
    normalized_payload: Dict[str, Any] = {
        "mode": payload_mode,
        "ignore_checksum": payload.get("ignore_checksum", False),
    }
    if not isinstance(normalized_payload["ignore_checksum"], bool):
        raise ConfigValidationError(
            "[update.payload].ignore_checksum must be a boolean"
        )
    extract_subdir = payload.get("extractSubdir")
    if extract_subdir is not None:
        if (
            not isinstance(extract_subdir, str)
            or Path(extract_subdir).is_absolute()
            or ".." in Path(extract_subdir).parts
        ):
            raise ConfigValidationError(
                "[update.payload].extractSubdir must be a safe relative path"
            )
        normalized_payload["extractSubdir"] = extract_subdir
    extract_mappings = payload.get("extract")
    if extract_mappings is not None:
        if payload_mode != "zip":
            raise ConfigValidationError(
                "[update.payload].extract is only supported with mode = 'zip'"
            )
        if extract_subdir is not None:
            raise ConfigValidationError(
                "[update.payload].extract cannot be combined with extractSubdir"
            )
        if not isinstance(extract_mappings, list):
            raise ConfigValidationError(
                "[update.payload].extract must be an array of tables"
            )
        if not extract_mappings:
            raise ConfigValidationError(
                "[update.payload].extract must contain at least one mapping"
            )
        normalized_mappings: List[Dict[str, str]] = []
        for index, mapping in enumerate(extract_mappings):
            context = f"update.payload.extract[{index}]"
            if not isinstance(mapping, dict):
                raise ConfigValidationError(f"[{context}] must be a table")
            _validate_exact_keys(
                mapping,
                allowed={"src", "dest"},
                context=context,
                ordered_allowed=["src", "dest"],
            )
            src = mapping.get("src")
            dest = mapping.get("dest")
            if not isinstance(src, str) or not src:
                raise ConfigValidationError(f"[{context}].src must be a non-empty string")
            if not isinstance(dest, str):
                raise ConfigValidationError(f"[{context}].dest must be a string")
            src_path = PureWindowsPath(src)
            dest_path = PureWindowsPath(dest)
            if (
                src_path.is_absolute()
                or src_path.drive
                or ".." in src_path.parts
            ):
                raise ConfigValidationError(
                    f"[{context}].src must be a safe path relative to the zip root"
                )
            if (
                dest_path.is_absolute()
                or dest_path.drive
                or ".." in dest_path.parts
                or any(character in dest for character in "*?[]")
            ):
                raise ConfigValidationError(
                    f"[{context}].dest must be a safe path relative to $App"
                )
            normalized_mappings.append({"src": src, "dest": dest})
        normalized_payload["extract"] = normalized_mappings
    rename_mappings = payload.get("rename")
    if rename_mappings is not None:
        if payload_mode != "zip":
            raise ConfigValidationError(
                "[update.payload].rename is only supported with mode = 'zip'"
            )
        if not isinstance(rename_mappings, list) or not rename_mappings:
            raise ConfigValidationError(
                "[update.payload].rename must be a non-empty array of tables"
            )
        normalized_renames: List[Dict[str, str]] = []
        for index, mapping in enumerate(rename_mappings):
            context = f"update.payload.rename[{index}]"
            if not isinstance(mapping, dict):
                raise ConfigValidationError(f"[{context}] must be a table")
            _validate_exact_keys(
                mapping,
                allowed={"src", "dest"},
                context=context,
                ordered_allowed=["src", "dest"],
            )
            src = mapping.get("src")
            dest = mapping.get("dest")
            if not isinstance(src, str) or not src:
                raise ConfigValidationError(f"[{context}].src must be a non-empty string")
            if not isinstance(dest, str) or not dest:
                raise ConfigValidationError(f"[{context}].dest must be a non-empty string")
            for field, value in (("src", src), ("dest", dest)):
                path = PureWindowsPath(value)
                if (
                    path.is_absolute()
                    or path.drive
                    or ".." in path.parts
                    or value in {".", ".."}
                    or any(character in value for character in "*?[]")
                ):
                    raise ConfigValidationError(
                        f"[{context}].{field} must be a safe path relative to $App"
                    )
            normalized_renames.append({"src": src, "dest": dest})
        normalized_payload["rename"] = normalized_renames
    if payload_mode == "module":
        module = payload.get("module", "pkg.local/unpack_app.py")
        if not isinstance(module, str):
            raise ConfigValidationError("[update.payload].module must be a string")
        normalized_payload["module"] = _validate_package_local_path(
            identity, module, context="update.payload"
        )

    # The historical check and payload tables remain the built-in update
    # stages.  An omitted step list therefore retains the established flow.
    raw_steps = raw_update.get("steps")
    if raw_steps is None:
        normalized_steps = [{"mode": "payload"}]
    else:
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ConfigValidationError("[[update.steps]] must contain at least one step")
        normalized_steps = []
        for index, step in enumerate(raw_steps):
            context = f"update.steps[{index}]"
            if not isinstance(step, dict):
                raise ConfigValidationError(f"[[{context}]] must be a table")
            step_mode = step.get("mode")
            if index == 0:
                _validate_exact_keys(
                    step,
                    allowed={"mode"},
                    context=context,
                    ordered_allowed=["mode"],
                )
                if step_mode != "payload":
                    raise ConfigValidationError(
                        "The first [[update.steps]] entry must use mode = 'payload'"
                    )
                normalized_steps.append({"mode": "payload"})
                continue
            _validate_exact_keys(
                step,
                allowed={"mode", "module"},
                context=context,
                ordered_allowed=["mode", "module"],
            )
            if step_mode != "module":
                raise ConfigValidationError(
                    f"[[{context}]].mode must be 'module' after the payload step"
                )
            module = step.get("module")
            if not isinstance(module, str):
                raise ConfigValidationError(f"[[{context}]].module must be a string")
            normalized_steps.append(
                {
                    "mode": "module",
                    "module": _validate_package_local_path(
                        identity, module, context=context
                    ),
                }
            )
    return {
        "check": normalized_check,
        "payload": normalized_payload,
        "steps": normalized_steps,
    }


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
        shape. The install path uses this one representation directly instead
        of translating into another layer of short-lived row objects.

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

    top_level_keys = [
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
    ]
    legacy_top_level_key_hints: Dict[str, Optional[str]] = {
        "env": "environment",
        "shortcuts": "shortcut",
        "portable": "only_portable",
        "onlyportable": "only_portable",
        "local_version": "localVersion",
        "downloadurl": "origin",
        "download_url": "origin",
        "main": None,
    }
    _validate_exact_keys(
        raw,
        allowed=set(top_level_keys),
        context="config",
        ordered_allowed=top_level_keys,
        legacy_hints=legacy_top_level_key_hints,
    )

    _normalize_optional_string(raw.get("name"), field_name="name")
    _normalize_optional_string(raw.get("version"), field_name="version")
    local_version = raw.get("localVersion")
    if local_version is not None:
        _normalize_local_version_value(local_version, field_name="localVersion")
    only_portable_value = raw.get("only_portable")
    normalized_only_portable = (
        identity.only_portable_by_name
        if only_portable_value is None
        else _normalize_only_portable_value(
            only_portable_value, field_name="only_portable"
        )
    )
    normalized_origin = normalize_origin_config(raw.get("origin"), identity.version)
    if normalized_origin is not None:
        origin_sources = [normalized_origin, *normalized_origin.get("versions", [])]
        for origin_source in origin_sources:
            if origin_source.get("mode") == "module":
                origin_source["module"] = _validate_package_local_path(
                    identity, origin_source["module"], context="origin"
                )
    normalized_update = normalize_update_config(
        raw.get("update"), identity, normalized_origin
    )
    if (
        normalized_origin is not None
        and normalized_origin.get("mode") == "git"
        and normalized_update is not None
        and normalized_update["check"]["mode"] == "git"
        and normalized_origin["ref"] != normalized_update["check"]["ref"]
    ):
        raise ConfigValidationError(
            "Git origin and update check must use the same ref"
        )

    environment_entries: List[Dict[str, str]] = []
    raw_environment = raw.get("environment")
    if raw_environment is not None:
        if not isinstance(raw_environment, list):
            raise ConfigValidationError(
                f"'environment' must be a list, got: {type(raw_environment).__name__}"
            )
        environment_keys = {"Name", "Value"}
        environment_legacy_key_hints = {"name": "Name", "value": "Value"}
        for index, item in enumerate(raw_environment):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'environment[{index}]' must be a table, got: {type(item).__name__}"
                )
            _validate_exact_keys(
                item,
                allowed=environment_keys,
                context=f"environment[{index}]",
                ordered_allowed=["Name", "Value"],
                legacy_hints=environment_legacy_key_hints,
            )
            environment_entries.append(
                {
                    "Name": _normalize_required_string(
                        item.get("Name"), field_name=f"environment[{index}].Name"
                    ),
                    "Value": _normalize_required_string(
                        item.get("Value"), field_name=f"environment[{index}].Value"
                    ),
                }
            )

    shortcut_entries: List[Dict[str, str]] = []
    raw_shortcut = raw.get("shortcut")
    if raw_shortcut is not None:
        if not isinstance(raw_shortcut, list):
            raise ConfigValidationError(
                f"'shortcut' must be a list, got: {type(raw_shortcut).__name__}"
            )
        shortcut_keys = {
            "name",
            "targetPath",
            "arguments",
            "workingDirectory",
            "iconLocation",
            "description",
        }
        shortcut_legacy_key_hints = {
            "path": "targetPath",
            "target_path": "targetPath",
            "args": "arguments",
            "workdir": "workingDirectory",
            "working_directory": "workingDirectory",
            "icon_location": "iconLocation",
            "desc": "description",
        }
        for index, item in enumerate(raw_shortcut):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'shortcut[{index}]' must be a table, got: {type(item).__name__}"
                )
            _validate_exact_keys(
                item,
                allowed=shortcut_keys,
                context=f"shortcut[{index}]",
                ordered_allowed=[
                    "name",
                    "targetPath",
                    "arguments",
                    "workingDirectory",
                    "iconLocation",
                    "description",
                ],
                legacy_hints=shortcut_legacy_key_hints,
            )
            shortcut_entries.append(
                {
                    "name": _normalize_required_string(
                        item.get("name"), field_name=f"shortcut[{index}].name"
                    ),
                    "targetPath": _normalize_required_string(
                        item.get("targetPath"),
                        field_name=f"shortcut[{index}].targetPath",
                    ),
                    "arguments": _normalize_required_string(
                        item.get("arguments"), field_name=f"shortcut[{index}].arguments"
                    ),
                    "workingDirectory": _normalize_required_string(
                        item.get("workingDirectory"),
                        field_name=f"shortcut[{index}].workingDirectory",
                    ),
                    "iconLocation": _normalize_required_string(
                        item.get("iconLocation"),
                        field_name=f"shortcut[{index}].iconLocation",
                    ),
                    "description": _normalize_required_string(
                        item.get("description"),
                        field_name=f"shortcut[{index}].description",
                    ),
                }
            )

    path_entries: List[str] = []
    raw_path_entries = raw.get("path")
    if raw_path_entries is not None:
        if not isinstance(raw_path_entries, list):
            raise ConfigValidationError(
                f"'path' must be a list of [[path]] tables, got: {type(raw_path_entries).__name__}"
            )
        path_keys = {"value"}
        path_legacy_key_hints = {"path": "value"}
        for index, item in enumerate(raw_path_entries):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'path[{index}]' must be a table, got: {type(item).__name__}"
                )
            _validate_exact_keys(
                item,
                allowed=path_keys,
                context=f"path[{index}]",
                ordered_allowed=["value"],
                legacy_hints=path_legacy_key_hints,
            )
            if "value" not in item:
                raise ConfigValidationError(
                    f"'path[{index}]' is missing required key: value"
                )
            value = item.get("value")
            if not isinstance(value, str):
                raise ConfigValidationError(
                    f"'path[{index}].value' must be a string, got: {type(value).__name__}"
                )
            path_entries.append(value)

    bin_entries: List[Dict[str, Any]] = []
    raw_bin = raw.get("bin")
    if raw_bin is not None:
        if not isinstance(raw_bin, list):
            raise ConfigValidationError(
                f"'bin' must be a list, got: {type(raw_bin).__name__}"
            )
        bin_keys = {
            "name",
            "target",
            "type",
            "arguments",
            "forward_args",
            "elevate",
            "working_dir",
            "content",
        }
        for index, item in enumerate(raw_bin):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"'bin[{index}]' must be a table, got: {type(item).__name__}"
                )
            _validate_exact_keys(
                item,
                allowed=bin_keys,
                context=f"bin[{index}]",
                ordered_allowed=[
                    "name",
                    "target",
                    "type",
                    "arguments",
                    "forward_args",
                    "elevate",
                    "working_dir",
                    "content",
                ],
            )
            content = _normalize_required_string(
                item.get("content"), field_name=f"bin[{index}].content"
            )
            target = _normalize_required_string(
                item.get("target"), field_name=f"bin[{index}].target"
            )
            shim_type = _normalize_optional_string(
                item.get("type"), field_name=f"bin[{index}].type"
            )
            if shim_type and shim_type not in {"console", "gui"}:
                raise ConfigValidationError(
                    f"'bin[{index}].type' must be 'console' or 'gui', "
                    f"got: {shim_type!r}"
                )

            arguments = item.get("arguments", [])
            if not isinstance(arguments, list) or any(
                not isinstance(argument, str) for argument in arguments
            ):
                raise ConfigValidationError(
                    f"'bin[{index}].arguments' must be an array of strings"
                )

            forward_args = item.get("forward_args", True)
            if not isinstance(forward_args, bool):
                raise ConfigValidationError(
                    f"'bin[{index}].forward_args' must be a boolean, "
                    f"got: {type(forward_args).__name__}"
                )
            elevate = item.get("elevate", False)
            if not isinstance(elevate, bool):
                raise ConfigValidationError(
                    f"'bin[{index}].elevate' must be a boolean, "
                    f"got: {type(elevate).__name__}"
                )
            working_dir = _normalize_optional_string(
                item.get("working_dir"), field_name=f"bin[{index}].working_dir"
            )

            shim_keys = {
                "target",
                "type",
                "arguments",
                "forward_args",
                "elevate",
                "working_dir",
            }
            if "content" in item and any(key in item for key in shim_keys):
                raise ConfigValidationError(
                    f"'bin[{index}].content' cannot be combined with shim options"
                )
            if "\n" not in content:
                content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
            normalized_bin: Dict[str, Any] = {
                "name": _normalize_required_string(
                    item.get("name"), field_name=f"bin[{index}].name"
                )
            }
            if "content" in item:
                normalized_bin["content"] = content
            else:
                normalized_bin.update(
                    {
                        "target": target,
                        "type": shim_type or "console",
                        "arguments": list(arguments),
                        "forward_args": forward_args,
                        "elevate": elevate,
                        "working_dir": working_dir,
                    }
                )
            bin_entries.append(normalized_bin)

    return {
        "description": _normalize_optional_string(
            raw.get("description"), field_name="description"
        ),
        "homepage": _normalize_optional_string(
            raw.get("homepage"), field_name="homepage"
        ),
        "origin": normalized_origin,
        "update": normalized_update,
        "only_portable": normalized_only_portable,
        "environment": environment_entries,
        "shortcut": shortcut_entries,
        "path": path_entries,
        "bin": bin_entries,
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
        If required fields are missing.

    """
    errors: List[str] = []
    for index, shortcut in enumerate(config["shortcut"]):
        missing = []
        if not shortcut.get("name", "").strip():
            missing.append("name")
        if not shortcut.get("targetPath", "").strip():
            missing.append("targetPath")
        if missing:
            errors.append(
                f"shortcut[{index}] missing required key(s): {', '.join(missing)}"
            )
    for index, env in enumerate(config["environment"]):
        missing = []
        if not env.get("Name", "").strip():
            missing.append("Name")
        if env.get("Value", "") == "":
            missing.append("Value")
        if missing:
            errors.append(
                f"environment[{index}] missing required key(s): {', '.join(missing)}"
            )
    for index, wrapper in enumerate(config["bin"]):
        missing = []
        if not wrapper.get("name", "").strip():
            missing.append("name")
        if wrapper.get("content", "") == "" and wrapper.get("target", "") == "":
            missing.append("target or content")
        if missing:
            errors.append(f"bin[{index}] missing required key(s): {', '.join(missing)}")
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
        A list of human-readable mismatch descriptions. The list is empty when
        the metadata is consistent.

    Raises
    ------
    TypeError
        If *raw_config* is not a dictionary.

    """
    if not isinstance(raw_config, dict):
        raise TypeError("raw_config must be a dict")

    inconsistencies: List[str] = []
    if "name" in raw_config and raw_config.get("name") not in (None, ""):
        # The directory owns the canonical spelling. Case-only differences are
        # the same package identity, while UpdateConfig writes the directory's
        # exact spelling back to package metadata when synchronization is run.
        configured_name = _normalize_optional_string(
            raw_config.get("name"), field_name="name"
        )
        if configured_name.casefold() != identity.name.casefold():
            inconsistencies.append(
                f"Name mismatch: directory='{identity.name}', config='{raw_config.get('name')}'"
            )
    if "version" in raw_config and raw_config.get("version") not in (None, ""):
        if (
            _normalize_optional_string(raw_config.get("version"), field_name="version")
            != identity.version
        ):
            inconsistencies.append(
                f"Version mismatch: directory='{identity.version}', config='{raw_config.get('version')}'"
            )
    if "localVersion" in raw_config and raw_config.get("localVersion") not in (
        None,
        "",
    ):
        normalized_local_version = _normalize_local_version_value(
            raw_config.get("localVersion"), field_name="localVersion"
        )
        if normalized_local_version != identity.local_version:
            inconsistencies.append(
                f"LocalVersion mismatch: directory='{identity.local_version}', config='{raw_config.get('localVersion')}'"
            )
    if "only_portable" in raw_config and raw_config.get("only_portable") is not None:
        normalized_only_portable = _normalize_only_portable_value(
            raw_config.get("only_portable"),
            field_name="only_portable",
        )
        if normalized_only_portable != identity.only_portable_by_name:
            inconsistencies.append(
                f"Portable flag mismatch: directory='{identity.only_portable_by_name}', config='{normalized_only_portable}'"
            )
    return inconsistencies


def read_runtime_config(
    identity: PackageIdentity, use_defaults: bool = False
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Read and validate ``pkg.toml`` for one package version.

    Parameters
    ----------
    identity : PackageIdentity
        Package identity whose ``pkg.toml`` should be loaded.
    use_defaults : bool
        Whether to fall back to defaults when parsing or
            validation fails.

    Returns
    -------
    Tuple[Dict[str, Any], Dict[str, Any], List[str]]
        A tuple ``(runtime_config, raw_dict, warnings)`` where ``raw_dict`` is
        the file-authored config when ``pkg.toml`` exists, or ``{}`` when no
        config is available.

    Raises
    ------
    ConfigValidationError
        If the config is invalid and *use_defaults* is
            ``False``.
    RuntimeError
        If the config cannot be read and *use_defaults* is
            ``False``.

    """
    warnings: List[str] = []
    toml_path = identity.version_path / "pkg.toml"
    if toml_path.exists():
        try:
            loaded = read_toml_file(toml_path)
            config = normalize_runtime_config(loaded, identity)
            validate_runtime_config(config)
            return config, dict(loaded), warnings
        except ConfigValidationError:
            raise
        except Exception as exc:
            if not use_defaults:
                raise RuntimeError(
                    f"Error loading TOML config from {toml_path}: {exc}"
                ) from exc
            warnings.append(f"Error loading TOML config from {toml_path}: {exc}")
            warnings.append(
                "Proceeding with defaults because --use-defaults was provided."
            )
    config = normalize_runtime_config({}, identity)
    validate_runtime_config(config)
    warnings.append(
        f"No pkg.toml found at {toml_path}; using defaults without creating a file."
    )
    return config, {}, warnings
