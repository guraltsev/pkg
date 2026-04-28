#!/usr/bin/env python3
"""Best-effort converter from legacy package config files to ``pkg.toml``.

The helper scans a directory for older JSON files and rewrites what it can into
one canonical ``pkg.toml`` file. It prefers producing current schema that
``pkg.py`` accepts today over preserving every old shape. Missing files,
partially malformed input, and uncertain metadata are treated as warnings rather
than hard failures, so manual review is still expected.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable


VERSION_DIR_NAME_RE = re.compile(r"^v(.+)\.l(\d+)$")
def new_config() -> dict[str, Any]:
    """Create an empty canonical config dictionary."""

    return {
        "name": None,
        "version": None,
        "localVersion": None,
        "only_portable": None,
        "description": None,
        "homepage": None,
        "downloadURL": None,
        "path": [],
        "environment": [],
        "shortcut": [],
        "bin": [],
    }


def to_toml_scalar(value: Any) -> str:
    """Render a Python value as TOML literal text.

    Args:
        value: Python scalar or list value to render.

    Returns:
        TOML literal text representing *value*.
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
        lines.extend([
            "[[path]]",
            f"value = {to_toml_scalar(entry)}",
            "",
        ])
    return lines


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file as a dictionary.

    Args:
        path: JSON file path to read.

    Returns:
        Parsed dictionary, or ``None`` when the file cannot be parsed into a
        JSON object.
    """

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: failed to read {path.name}: {exc}")
        return None
    if not isinstance(parsed, dict):
        print(f"Warning: {path.name} is not a JSON object; ignoring")
        return None
    return parsed


def find_legacy_file(base_dir: Path, exact_name: str) -> Path | None:
    """Find a legacy file by exact or case-insensitive name."""

    exact = base_dir / exact_name
    if exact.exists():
        return exact

    target = exact_name.lower()
    for candidate in sorted(base_dir.glob("*.json")):
        if candidate.name.lower() == target:
            return candidate
    return None


def pick_all_matching(base_dir: Path, prefix: str) -> list[Path]:
    """Collect all legacy JSON files that share a prefix."""

    files: list[Path] = []
    for candidate in sorted(base_dir.glob("*.json")):
        lower = candidate.name.lower()
        if lower == f"{prefix}.json" or (lower.startswith(prefix) and lower.endswith(".json")):
            files.append(candidate)
    return files


def normalize_legacy_variable_path(value: str) -> str:
    """Normalize legacy package-variable spellings to canonical forms."""

    normalized = value
    component_patterns = [
        (r"\$AppPath[\\/]+App(?=[\\/]+|$)", "$App"),
        (r"\$AppPath[\\/]+Icons(?=[\\/]+|$)", "$Icons"),
        (r"\$AppPath[\\/]+Shortcuts(?=[\\/]+|$)", "$Shortcuts"),
        (r"\$(?:Pkg_App_Path|PkgAppPath|Package_App_Path)(?=[\\/]+|$)", "$App"),
        (r"\$(?:Pkg_Icons_Path|PkgIconsPath|Package_Icons_Path)(?=[\\/]+|$)", "$Icons"),
        (r"\$(?:Pkg_Shortcuts_Path|PkgShortcutsPath|Package_Shortcuts_Path)(?=[\\/]+|$)", "$Shortcuts"),
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
        print(f"Warning: {source_name}: '{label}' should be an integer; ignoring {value!r}")
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    print(f"Warning: {source_name}: '{label}' should be an integer; ignoring {value!r}")
    return None


def _normalize_optional_bool(value: Any, *, label: str, source_name: str) -> bool | None:
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
    source: dict[str, Any],
    source_keys: list[str],
    dest_key: str,
    normalizer: Callable[[dict[str, Any]], dict[str, str]],
    source_name: str,
) -> None:
    """Normalize one or more legacy list blocks and append valid rows."""

    for source_key in source_keys:
        rows = source.get(source_key)
        if rows is None:
            continue
        if not isinstance(rows, list):
            print(f"Warning: {source_name}: '{source_key}' should be a list")
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = normalizer(row)
            if normalized:
                output[dest_key].append(normalized)


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

    Args:
        base_dir: Directory that contains legacy JSON files.

    Returns:
        Canonical configuration dictionary.
    """

    out = new_config()
    inferred = infer_metadata_from_directory(base_dir)

    opt_pkg = find_legacy_file(base_dir, "opt_pkg.json")
    if opt_pkg:
        data = read_json(opt_pkg)
        if data:
            top_map = {
                "name": "name",
                "version": "version",
                "description": "description",
                "homepage": "homepage",
                "downloadurl": "downloadURL",
                "download_url": "downloadURL",
            }
            for key, value in data.items():
                canon = top_map.get(str(key).lower())
                if canon is None or value in (None, ""):
                    continue
                out[canon] = str(value)

            local_value = data.get("localVersion")
            if local_value is None:
                local_value = data.get("local_version")
            out["localVersion"] = _normalize_optional_int(local_value, label="localVersion", source_name=opt_pkg.name)

            portable_value = data.get("only_portable")
            if portable_value is None:
                portable_value = data.get("onlyportable")
            if portable_value is None:
                portable_value = data.get("portable")
            out["only_portable"] = _normalize_optional_bool(
                portable_value,
                label="only_portable",
                source_name=opt_pkg.name,
            )

            raw_path_entries = data.get("path")
            if isinstance(raw_path_entries, list):
                out["path"] = [normalize_legacy_variable_path(str(item)) for item in raw_path_entries if item is not None]
            elif raw_path_entries is not None:
                print(f"Warning: {opt_pkg.name}: 'path' should be a list")

            extend_normalized_list(out, data, ["environment", "env"], "environment", normalize_environment, opt_pkg.name)
            extend_normalized_list(out, data, ["shortcut"], "shortcut", normalize_shortcut, opt_pkg.name)
            extend_normalized_list(out, data, ["bin"], "bin", normalize_bin, opt_pkg.name)

    for env_file in [*pick_all_matching(base_dir, "environment"), *pick_all_matching(base_dir, "env")]:
        data = read_json(env_file)
        if not data:
            continue
        extend_normalized_list(out, data, ["environment", "env"], "environment", normalize_environment, env_file.name)

    for shortcut_file in pick_all_matching(base_dir, "shortcut"):
        data = read_json(shortcut_file)
        if not data:
            continue
        extend_normalized_list(out, data, ["shortcut"], "shortcut", normalize_shortcut, shortcut_file.name)

    for bin_file in pick_all_matching(base_dir, "bin"):
        data = read_json(bin_file)
        if not data:
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
    for key in ["description", "homepage", "downloadURL"]:
        if cfg.get(key) not in (None, ""):
            lines.append(f"{key} = {to_toml_scalar(cfg[key])}")
    if lines[-1] != "":
        lines.append("")

    lines.extend(toml_path_lines(cfg.get("path", [])))

    for entry in cfg.get("environment", []):
        lines.extend([
            "[[environment]]",
            f"Name = {to_toml_scalar(entry.get('Name', ''))}",
            f"Value = {to_toml_scalar(entry.get('Value', ''))}",
            "",
        ])

    for entry in cfg.get("shortcut", []):
        lines.append("[[shortcut]]")
        for key in ["name", "targetPath", "arguments", "workingDirectory", "iconLocation", "description"]:
            if key in entry and entry.get(key) not in (None, ""):
                lines.append(f"{key} = {to_toml_scalar(entry[key])}")
        lines.append("")

    for entry in cfg.get("bin", []):
        lines.extend([
            "[[bin]]",
            f"name = {to_toml_scalar(entry.get('name', ''))}",
            f"content = {to_toml_scalar(entry.get('content', ''))}",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def write_pkg_toml(path: Path, cfg: dict[str, Any]) -> None:
    """Write canonical ``pkg.toml`` text to *path*."""

    path.write_text(render_pkg_toml(cfg), encoding="utf-8")


def main() -> int:
    """Run the legacy-to-TOML converter CLI.

    Returns:
        Process exit code.
    """

    parser = argparse.ArgumentParser(description="Construct canonical pkg.toml from legacy files in one directory")
    parser.add_argument("--dir", default=".", help="Directory that contains legacy JSON files (default: current directory)")
    parser.add_argument("--output", default="pkg.toml", help="Output TOML path (default: ./pkg.toml)")
    parser.add_argument("--dry-run", action="store_true", help="Print resulting TOML to stdout instead of writing")
    parser.add_argument("--force", action="store_true", help="Overwrite output file if it already exists")
    args = parser.parse_args()

    base_dir = Path(args.dir).resolve()
    cfg = build_config(base_dir)

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()

    if args.dry_run:
        print(render_pkg_toml(cfg), end="")
        return 0

    if out_path.exists() and not args.force:
        print(
            f"Error: {out_path} already exists. "
            "Refusing to overwrite existing TOML to avoid data loss. "
            "Use --force to overwrite."
        )
        return 1

    write_pkg_toml(out_path, cfg)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
