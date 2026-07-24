#!/usr/bin/env python3
"""Convert legacy package metadata files into one canonical ``pkg.toml``.

Call ``main()`` to scan one package version directory for older JSON or TOML
files and emit current-schema ``pkg.toml`` content. The converter keeps working
through missing files, malformed legacy sections, and uncertain metadata by
printing warnings so callers can review the result before relying on it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any, Callable


VERSION_DIR_NAME_RE = re.compile(r"^v(.+)\.l(\d+)$")

# Legacy package metadata commonly lived in one of these filenames.
LEGACY_METADATA_FILENAMES = [
    "opt_pkg.json",
    "package.json",
    "pkg.json",
    "pkg.toml",
]


def new_config() -> dict[str, Any]:
    """Create an empty canonical config dictionary."""

    return {
        "name": None,
        "version": None,
        "localVersion": None,
        "only_portable": None,
        "description": None,
        "homepage": None,
        "origin": None,
        "path": [],
        "environment": [],
        "shortcut": [],
        "bin": [],
    }


def to_toml_scalar(value: Any) -> str:
    """Render a Python value as TOML literal text.

    Parameters
    ----------
    value : Any
        Python scalar or list value to render.

    Returns
    -------
    str
        TOML literal text representing ``value``.
    """

    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(to_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def toml_path_lines(path_entries: list[str]) -> list[str]:
    """Render canonical ``[[path]]`` tables for a list of PATH entries."""

    lines: list[str] = []
    for entry in path_entries:
        lines.extend(
            [
                "[[path]]",
                f"value = {to_toml_scalar(entry)}",
                "",
            ]
        )
    return lines


def read_legacy_data(path: Path) -> Any | None:
    """Read a legacy JSON or TOML file.

    Parameters
    ----------
    path : Path
        Legacy config file path to read.

    Returns
    -------
    Any | None
        Parsed object, or ``None`` when the file cannot be parsed.
    """

    try:
        if path.suffix.lower() == ".toml":
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: failed to read {path.name}: {exc}")
        return None
    return parsed


def get_matching_values(source: dict[str, Any], candidate_keys: list[str]) -> list[Any]:
    """Return values for keys that match one of the aliases case-insensitively."""

    wanted = {key.lower() for key in candidate_keys}
    return [value for key, value in source.items() if str(key).lower() in wanted]


def find_legacy_file(base_dir: Path, exact_name: str) -> Path | None:
    """Find a legacy file by exact or case-insensitive name."""

    exact = base_dir / exact_name
    if exact.exists():
        return exact

    target = exact_name.lower()
    for candidate in sorted(base_dir.iterdir()):
        if not candidate.is_file():
            continue
        if candidate.name.lower() == target:
            return candidate
    return None


def pick_all_matching(base_dir: Path, prefix: str) -> list[Path]:
    """Collect all legacy files that share a prefix."""

    files: list[Path] = []
    for candidate in sorted(base_dir.iterdir()):
        if not candidate.is_file():
            continue
        lower = candidate.name.lower()
        if lower == f"{prefix}.json" or lower == f"{prefix}.toml":
            files.append(candidate)
            continue
        if lower.startswith(prefix) and (
            lower.endswith(".json") or lower.endswith(".toml")
        ):
            files.append(candidate)
    return files


def pick_legacy_metadata_files(base_dir: Path) -> list[Path]:
    """Collect known legacy metadata filenames without duplicates."""

    found: list[Path] = []
    seen: set[Path] = set()
    for filename in LEGACY_METADATA_FILENAMES:
        path = find_legacy_file(base_dir, filename)
        if path is None or path in seen:
            continue
        found.append(path)
        seen.add(path)
    return found


def extract_legacy_rows(
    source: Any, source_keys: list[str], *, source_name: str
) -> list[dict[str, Any]]:
    """Extract list-style legacy rows from a mapping or list payload."""

    candidate_lists: list[Any] = []
    if isinstance(source, list):
        candidate_lists.append(source)
    elif isinstance(source, dict):
        candidate_lists.extend(
            value
            for value in get_matching_values(source, source_keys)
            if value is not None
        )
    else:
        print(f"Warning: {source_name} is not an object or list; ignoring")
        return []

    rows: list[dict[str, Any]] = []
    for candidate in candidate_lists:
        if not isinstance(candidate, list):
            joined_keys = ", ".join(source_keys)
            print(
                f"Warning: {source_name}: expected one of [{joined_keys}] to be a list"
            )
            continue
        for row in candidate:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def normalize_legacy_variable_path(value: str) -> str:
    """Normalize legacy package-variable spellings to canonical forms."""

    normalized = value
    component_patterns = [
        (r"\$AppPath[\\/]+App(?=[\\/]+|$)", "$App"),
        (r"\$AppPath[\\/]+Icons(?=[\\/]+|$)", "$Icons"),
        (r"\$AppPath[\\/]+Shortcuts(?=[\\/]+|$)", "$Shortcuts"),
        (r"\$(?:Pkg_App_Path|PkgAppPath|Package_App_Path)(?=[\\/]+|$)", "$App"),
        (r"\$(?:Pkg_Icons_Path|PkgIconsPath|Package_Icons_Path)(?=[\\/]+|$)", "$Icons"),
        (
            r"\$(?:Pkg_Shortcuts_Path|PkgShortcutsPath|Package_Shortcuts_Path)(?=[\\/]+|$)",
            "$Shortcuts",
        ),
        (r"\$AppPath(?=[\\/]+|$)", "$App"),
    ]
    for pattern, replacement in component_patterns:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


def _normalize_optional_int(value: Any, *, label: str, source_name: str) -> int | None:
    """Normalize a best-effort integer metadata value."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        print(
            f"Warning: {source_name}: '{label}' should be an integer; ignoring {value!r}"
        )
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    print(f"Warning: {source_name}: '{label}' should be an integer; ignoring {value!r}")
    return None


def _normalize_optional_bool(
    value: Any, *, label: str, source_name: str
) -> bool | None:
    """Normalize a best-effort boolean metadata value."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    print(f"Warning: {source_name}: '{label}' should be a boolean; ignoring {value!r}")
    return None


def normalize_shortcut(item: dict[str, Any]) -> dict[str, str]:
    """Normalize one legacy shortcut declaration."""

    key_map = {
        "name": "name",
        "targetpath": "targetPath",
        "target_path": "targetPath",
        "path": "targetPath",
        "arguments": "arguments",
        "args": "arguments",
        "workingdirectory": "workingDirectory",
        "working_directory": "workingDirectory",
        "workdir": "workingDirectory",
        "iconlocation": "iconLocation",
        "icon_location": "iconLocation",
        "description": "description",
        "desc": "description",
    }

    out: dict[str, str] = {}
    for key, value in item.items():
        canon = key_map.get(str(key).lower())
        if canon is None or value is None:
            continue
        out[canon] = normalize_legacy_variable_path(str(value))

    if out.get("name", "").lower().endswith(".lnk"):
        out["name"] = out["name"][:-4]

    if not out.get("name") or not out.get("targetPath"):
        return {}
    return out


def normalize_environment(item: dict[str, Any]) -> dict[str, str]:
    """Normalize one legacy environment-variable declaration."""

    key_map = {
        "name": "Name",
        "value": "Value",
    }
    out: dict[str, str] = {}
    for key, value in item.items():
        canon = key_map.get(str(key).lower())
        if canon is None or value is None:
            continue
        out[canon] = normalize_legacy_variable_path(str(value))
    if not out.get("Name") or out.get("Value", "") == "":
        return {}
    return out


def normalize_bin(item: dict[str, Any]) -> dict[str, str]:
    """Normalize one legacy wrapper declaration."""

    name = ""
    content = ""
    for key, value in item.items():
        lower_key = str(key).lower()
        if lower_key == "name" and value is not None:
            name = str(value)
        elif lower_key == "content" and value is not None:
            content = normalize_legacy_variable_path(str(value))
    if not name or content == "":
        return {}
    return {"name": name, "content": content}


def extend_normalized_list(
    output: dict[str, Any],
    source: Any,
    source_keys: list[str],
    dest_key: str,
    normalizer: Callable[[dict[str, Any]], dict[str, str]],
    source_name: str,
) -> None:
    """Normalize one or more legacy list blocks and append valid rows."""

    for row in extract_legacy_rows(source, source_keys, source_name=source_name):
        normalized = normalizer(row)
        if normalized:
            output[dest_key].append(normalized)


def apply_top_level_metadata(
    output: dict[str, Any], source: dict[str, Any], *, source_name: str
) -> None:
    """Copy best-effort top-level metadata from one legacy mapping."""

    top_map = {
        "name": "name",
        "version": "version",
        "description": "description",
        "homepage": "homepage",
    }
    for key, value in source.items():
        canon = top_map.get(str(key).lower())
        if canon is None or value in (None, ""):
            continue
        output[canon] = str(value)

    origin_values = get_matching_values(source, ["origin"])
    if origin_values and isinstance(origin_values[-1], dict):
        output["origin"] = dict(origin_values[-1])

    download_values = get_matching_values(source, ["downloadURL", "download_url"])
    if download_values and download_values[-1] not in (None, ""):
        # Retain canonical origin options while moving a legacy URL into the
        # current source field.
        origin = output.get("origin")
        output["origin"] = dict(origin) if isinstance(origin, dict) else {}
        output["origin"]["url"] = str(download_values[-1])

    local_values = get_matching_values(source, ["localVersion", "local_version"])
    if local_values:
        output["localVersion"] = _normalize_optional_int(
            local_values[-1],
            label="localVersion",
            source_name=source_name,
        )

    portable_values = get_matching_values(
        source, ["only_portable", "onlyportable", "portable"]
    )
    if portable_values:
        output["only_portable"] = _normalize_optional_bool(
            portable_values[-1],
            label="only_portable",
            source_name=source_name,
        )


def normalize_path_entries(raw_entries: Any, *, source_name: str) -> list[str]:
    """Normalize legacy PATH entries from strings or old table aliases."""

    if raw_entries is None:
        return []
    if not isinstance(raw_entries, list):
        print(f"Warning: {source_name}: 'path' should be a list")
        return []

    normalized: list[str] = []
    for item in raw_entries:
        if item is None:
            continue
        if isinstance(item, str):
            normalized.append(normalize_legacy_variable_path(item))
            continue
        if isinstance(item, dict):
            values = get_matching_values(item, ["value", "path"])
            if values:
                normalized.append(normalize_legacy_variable_path(str(values[-1])))
            continue
    return normalized


def extract_metadata_sources(
    source: dict[str, Any], *, source_name: str
) -> list[dict[str, Any]]:
    """Return top-level and legacy ``main`` metadata mappings from one payload."""

    metadata_sources = [source]
    for main_value in get_matching_values(source, ["main"]):
        if isinstance(main_value, dict):
            metadata_sources.append(main_value)
            continue
        if isinstance(main_value, list):
            dict_rows = [row for row in main_value if isinstance(row, dict)]
            if len(dict_rows) > 1:
                print(
                    f"Warning: {source_name}: found multiple 'main' tables; using the last one"
                )
            if dict_rows:
                metadata_sources.append(dict_rows[-1])
            continue
        print(f"Warning: {source_name}: 'main' should be a table or list of tables")
    return metadata_sources


def merge_legacy_source(
    output: dict[str, Any], source: Any, *, source_name: str
) -> None:
    """Merge one legacy JSON or TOML payload into the canonical config."""

    list_sources = [source]
    if isinstance(source, dict):
        metadata_sources = extract_metadata_sources(source, source_name=source_name)
        for metadata_source in metadata_sources:
            apply_top_level_metadata(output, metadata_source, source_name=source_name)
        list_sources.extend(metadata_sources[1:])

    for list_source in list_sources:
        if isinstance(list_source, dict):
            path_values = get_matching_values(list_source, ["path"])
            if path_values:
                output["path"] = normalize_path_entries(
                    path_values[-1], source_name=source_name
                )

        extend_normalized_list(
            output,
            list_source,
            ["environment", "env"],
            "environment",
            normalize_environment,
            source_name,
        )
        extend_normalized_list(
            output,
            list_source,
            ["shortcut", "shortcuts"],
            "shortcut",
            normalize_shortcut,
            source_name,
        )
        extend_normalized_list(
            output, list_source, ["bin"], "bin", normalize_bin, source_name
        )


def infer_metadata_from_directory(base_dir: Path) -> dict[str, Any]:
    """Infer metadata from a conventional version-directory path."""

    inferred = new_config()
    match = VERSION_DIR_NAME_RE.match(base_dir.name)
    if not match:
        return inferred

    package_name = base_dir.parent.name
    inferred["name"] = package_name or None
    inferred["version"] = match.group(1)
    inferred["localVersion"] = int(match.group(2))
    inferred["only_portable"] = package_name.lower().endswith("-portable")
    return inferred


def merge_missing_metadata(output: dict[str, Any], inferred: dict[str, Any]) -> None:
    """Fill unset metadata fields from inferred values."""

    for key in ["name", "version", "localVersion", "only_portable"]:
        if output.get(key) is None and inferred.get(key) is not None:
            output[key] = inferred[key]


def build_config(base_dir: Path) -> dict[str, Any]:
    """Build a canonical ``pkg.toml``-style config from legacy files.

    Parameters
    ----------
    base_dir : Path
        Directory that contains legacy JSON and TOML files.

    Returns
    -------
    dict[str, Any]
        Canonical configuration dictionary assembled from the discovered
        sources.
    """

    out = new_config()
    inferred = infer_metadata_from_directory(base_dir)

    # Merge primary metadata files first so explicit package identity and
    # origin fields are established before section-specific files add rows.
    for metadata_file in pick_legacy_metadata_files(base_dir):
        data = read_legacy_data(metadata_file)
        if data is not None:
            merge_legacy_source(out, data, source_name=metadata_file.name)

    # Legacy repos often split repeated tables into dedicated files. Extend the
    # canonical lists in discovery order so later review sees every recovered
    # declaration in one config.
    for env_file in [
        *pick_all_matching(base_dir, "environment"),
        *pick_all_matching(base_dir, "env"),
    ]:
        data = read_legacy_data(env_file)
        if data is None:
            continue
        extend_normalized_list(
            out,
            data,
            ["environment", "env"],
            "environment",
            normalize_environment,
            env_file.name,
        )

    for path_file in pick_all_matching(base_dir, "path"):
        data = read_legacy_data(path_file)
        if data is None:
            continue
        if isinstance(data, dict):
            path_values = get_matching_values(data, ["path"])
            if path_values:
                out["path"].extend(
                    normalize_path_entries(path_values[-1], source_name=path_file.name)
                )
        else:
            out["path"].extend(normalize_path_entries(data, source_name=path_file.name))

    for shortcut_file in [
        *pick_all_matching(base_dir, "shortcut"),
        *pick_all_matching(base_dir, "shortcuts"),
    ]:
        data = read_legacy_data(shortcut_file)
        if data is None:
            continue
        extend_normalized_list(
            out,
            data,
            ["shortcut", "shortcuts"],
            "shortcut",
            normalize_shortcut,
            shortcut_file.name,
        )

    for bin_file in pick_all_matching(base_dir, "bin"):
        data = read_legacy_data(bin_file)
        if data is None:
            continue
        extend_normalized_list(out, data, ["bin"], "bin", normalize_bin, bin_file.name)

    merge_missing_metadata(out, inferred)
    return out


def render_pkg_toml(cfg: dict[str, Any]) -> str:
    """Render canonical ``pkg.toml`` text for a config dictionary."""

    lines = [
        "# Auto-generated from legacy config files.",
        "# Please review and edit as needed.",
        "",
    ]

    if cfg.get("name") not in (None, ""):
        lines.append(f"name = {to_toml_scalar(cfg['name'])}")
    if cfg.get("version") not in (None, ""):
        lines.append(f"version = {to_toml_scalar(cfg['version'])}")
    if cfg.get("localVersion") is not None:
        lines.append(f"localVersion = {to_toml_scalar(cfg['localVersion'])}")
    if cfg.get("only_portable") is not None:
        lines.append(f"only_portable = {to_toml_scalar(cfg['only_portable'])}")
    for key in ["description", "homepage"]:
        if cfg.get(key) not in (None, ""):
            lines.append(f"{key} = {to_toml_scalar(cfg[key])}")
    if lines[-1] != "":
        lines.append("")

    origin = cfg.get("origin")
    if isinstance(origin, dict):
        # Copy supported current-source settings before preserving versioned
        # history entries as their canonical repeated TOML tables.
        origin_fields = ["url", "checksum", "extractSubdir", "script"]
        version_entries = origin.get("versions")
        if any(
            origin.get(key) not in (None, "") for key in origin_fields
        ) or isinstance(version_entries, list):
            lines.append("[origin]")
            for key in origin_fields:
                if origin.get(key) not in (None, ""):
                    lines.append(f"{key} = {to_toml_scalar(origin[key])}")
            if isinstance(version_entries, list):
                for entry in version_entries:
                    if not isinstance(entry, dict):
                        continue
                    lines.append("")
                    lines.append("[[origin.versions]]")
                    for key in [
                        "version",
                        "url",
                        "checksum",
                        "extractSubdir",
                        "script",
                    ]:
                        if entry.get(key) not in (None, ""):
                            lines.append(f"{key} = {to_toml_scalar(entry[key])}")
        lines.append("")

    lines.extend(toml_path_lines(cfg.get("path", [])))

    for entry in cfg.get("environment", []):
        lines.extend(
            [
                "[[environment]]",
                f"Name = {to_toml_scalar(entry.get('Name', ''))}",
                f"Value = {to_toml_scalar(entry.get('Value', ''))}",
                "",
            ]
        )

    for entry in cfg.get("shortcut", []):
        lines.append("[[shortcut]]")
        for key in [
            "name",
            "targetPath",
            "arguments",
            "workingDirectory",
            "iconLocation",
            "description",
        ]:
            if key in entry and entry.get(key) not in (None, ""):
                lines.append(f"{key} = {to_toml_scalar(entry[key])}")
        lines.append("")

    for entry in cfg.get("bin", []):
        lines.extend(
            [
                "[[bin]]",
                f"name = {to_toml_scalar(entry.get('name', ''))}",
                f"content = {to_toml_scalar(entry.get('content', ''))}",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def write_pkg_toml(path: Path, cfg: dict[str, Any]) -> None:
    """Write canonical ``pkg.toml`` text to *path*."""

    path.write_text(render_pkg_toml(cfg), encoding="utf-8")


def convert_legacy_directory(
    base_dir: Path,
    output_path: Path,
    *,
    dry_run: bool = False,
) -> bool:
    """Convert legacy package files into one canonical TOML document.

    Parameters
    ----------
    base_dir : Path
        Directory containing the legacy package files to inspect.
    output_path : Path
        Destination for the generated canonical TOML document.
    dry_run : bool, default=False
        Whether to print generated TOML instead of writing the destination.

    Returns
    -------
    bool
        ``True`` when the destination was written and ``False`` for a dry run.

    Notes
    -----
    An existing destination is copied to the next available ``.bak`` path
    before replacement.
    """

    # Build the complete canonical representation before touching an existing
    # destination, so malformed inputs cannot leave partial output.
    cfg = build_config(base_dir)
    if dry_run:
        print(render_pkg_toml(cfg), end="")
        return False

    # Preserve every previous conversion result under a numbered backup rather
    # than overwriting the reviewable result from an earlier migration.
    if output_path.exists():
        backup_path = output_path.with_name(output_path.name + ".bak")
        backup_number = 1
        while backup_path.exists():
            backup_path = output_path.with_name(
                output_path.name + f".bak.{backup_number}"
            )
            backup_number += 1
        shutil.copy2(output_path, backup_path)
        print(f"Backed up {output_path} to {backup_path}")

    write_pkg_toml(output_path, cfg)
    print(f"Wrote {output_path}")
    return True


def main() -> int:
    """Run the legacy-to-TOML converter CLI.

    Returns
    -------
    int
        Process exit code.
    """

    parser = argparse.ArgumentParser(
        description="Construct canonical pkg.toml from legacy files in one directory"
    )
    parser.add_argument(
        "--dir",
        default=".",
        help="Directory that contains legacy JSON files (default: current directory)",
    )
    parser.add_argument(
        "--output", default="pkg.toml", help="Output TOML path (default: ./pkg.toml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resulting TOML to stdout instead of writing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deprecated compatibility option; existing output is backed up and replaced by default",
    )
    args = parser.parse_args()

    base_dir = Path(args.dir).resolve()
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()

    convert_legacy_directory(base_dir, out_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
