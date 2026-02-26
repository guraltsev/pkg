#!/usr/bin/env python3
"""Best-effort converter from legacy package config files to pkg.toml.

The script scans the current directory for known legacy JSON files and merges
what it can into a single ``pkg.toml``:

- ``opt_pkg.json``: package metadata (name/version/description/homepage/...)
- ``shortcut*.json``: legacy shortcut declarations
- ``bin*.json``: legacy bin wrapper declarations

It is intentionally tolerant and keeps going when some files are missing or
partially malformed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "name": "",
    "version": "",
    "localVersion": "",
    "only_portable": False,
    "description": "",
    "homepage": "",
    "downloadURL": "",
    "path": [],
    "environment": [],
    "shortcut": [],
    "bin": [],
}


def to_toml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(to_toml_scalar(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def toml_path_lines(path_entries: list[Any]) -> list[str]:
    if not path_entries:
        return ["path = []"]

    lines = ["path = ["]
    for entry in path_entries:
        lines.append(f"  {to_toml_scalar(str(entry))},")
    lines.append("]")
    return lines


def read_json(path: Path) -> dict[str, Any] | None:
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
    # Prefer exact filename, then case-insensitive fallback.
    exact = base_dir / exact_name
    if exact.exists():
        return exact

    target = exact_name.lower()
    for candidate in sorted(base_dir.glob("*.json")):
        if candidate.name.lower() == target:
            return candidate
    return None


def pick_all_matching(base_dir: Path, prefix: str) -> list[Path]:
    files: list[Path] = []
    for candidate in sorted(base_dir.glob("*.json")):
        lower = candidate.name.lower()
        if lower == f"{prefix}.json" or (lower.startswith(prefix) and lower.endswith(".json")):
            files.append(candidate)
    return files


def normalize_shortcut(item: dict[str, Any]) -> dict[str, str]:
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
    for k, v in item.items():
        canon = key_map.get(str(k).lower())
        if canon is None or v is None:
            continue
        out[canon] = normalize_legacy_variable_path(str(v))

    # best effort for legacy .lnk naming
    if out.get("name", "").lower().endswith(".lnk"):
        out["name"] = out["name"][:-4]

    # only keep useful rows
    if not out.get("name") or not out.get("targetPath"):
        return {}
    return out


def normalize_bin(item: dict[str, Any]) -> dict[str, str]:
    name = ""
    content = ""
    for k, v in item.items():
        lk = str(k).lower()
        if lk == "name" and v is not None:
            name = str(v)
        elif lk == "content" and v is not None:
            content = normalize_legacy_variable_path(str(v))
    if not name or content == "":
        return {}
    return {"name": name, "content": content}


def normalize_legacy_variable_path(value: str) -> str:
    """Normalize legacy path variable spellings to canonical forms.

    Legacy configs can reference package component directories through
    ``$AppPath``-style variables. Canonicalize these to current package
    variables (``$App``, ``$Icons``, ``$Shortcuts``).
    """

    normalized = value

    component_patterns = [
        # Legacy root + explicit component folder.
        (r"\$AppPath[\\/]+App(?=[\\/]+|$)", "$App"),
        (r"\$AppPath[\\/]+Icons(?=[\\/]+|$)", "$Icons"),
        (r"\$AppPath[\\/]+Shortcuts(?=[\\/]+|$)", "$Shortcuts"),
        # Legacy component-specific aliases.
        (r"\$(?:Pkg_App_Path|PkgAppPath|Package_App_Path)(?=[\\/]+|$)", "$App"),
        (r"\$(?:Pkg_Icons_Path|PkgIconsPath|Package_Icons_Path)(?=[\\/]+|$)", "$Icons"),
        (r"\$(?:Pkg_Shortcuts_Path|PkgShortcutsPath|Package_Shortcuts_Path)(?=[\\/]+|$)", "$Shortcuts"),
        # Bare AppPath points to App component in old configs.
        (r"\$AppPath(?=[\\/]+|$)", "$App"),
    ]

    for pattern, replacement in component_patterns:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    return normalized


def extend_normalized_list(
    output: dict[str, Any],
    source: dict[str, Any],
    key: str,
    normalizer,
    source_name: str,
) -> None:
    rows = source.get(key, [])
    if rows is None:
        return
    if not isinstance(rows, list):
        print(f"Warning: {source_name}: '{key}' should be a list")
        return

    for row in rows:
        if not isinstance(row, dict):
            continue
        norm = normalizer(row)
        if norm:
            output[key].append(norm)


def build_config(base_dir: Path) -> dict[str, Any]:
    out = dict(DEFAULTS)

    opt_pkg = find_legacy_file(base_dir, "opt_pkg.json")
    if opt_pkg:
        data = read_json(opt_pkg)
        if data:
            top_map = {
                "name": "name",
                "version": "version",
                "localversion": "localVersion",
                "local_version": "localVersion",
                "description": "description",
                "homepage": "homepage",
                "downloadurl": "downloadURL",
                "download_url": "downloadURL",
                "only_portable": "only_portable",
                "onlyportable": "only_portable",
                "portable": "only_portable",
                "path": "path",
            }
            for k, v in data.items():
                canon = top_map.get(str(k).lower())
                if canon is None or v is None:
                    continue
                if canon == "path" and isinstance(v, str):
                    out["path"] = [v]
                elif canon == "path" and isinstance(v, list):
                    out["path"] = [str(x) for x in v if x is not None]
                else:
                    out[canon] = v

            # Some legacy repositories stored list blocks directly in opt_pkg.json
            # rather than separate shortcut*.json / bin*.json files.
            extend_normalized_list(out, data, "shortcut", normalize_shortcut, opt_pkg.name)
            extend_normalized_list(out, data, "bin", normalize_bin, opt_pkg.name)

    for shortcut_file in pick_all_matching(base_dir, "shortcut"):
        data = read_json(shortcut_file)
        if not data:
            continue
        extend_normalized_list(out, data, "shortcut", normalize_shortcut, shortcut_file.name)

    for bin_file in pick_all_matching(base_dir, "bin"):
        data = read_json(bin_file)
        if not data:
            continue
        extend_normalized_list(out, data, "bin", normalize_bin, bin_file.name)

    return out


def write_pkg_toml(path: Path, cfg: dict[str, Any]) -> None:
    lines = [
        "# Auto-generated from legacy config files.",
        "# Please review and edit as needed.",
        "[[main]]",
        f"name = {to_toml_scalar(cfg.get('name', ''))}",
        f"version = {to_toml_scalar(cfg.get('version', ''))}",
        f"localVersion = {to_toml_scalar(cfg.get('localVersion', ''))}",
        f"only_portable = {to_toml_scalar(cfg.get('only_portable', False))}",
        f"description = {to_toml_scalar(cfg.get('description', ''))}",
        f"homepage = {to_toml_scalar(cfg.get('homepage', ''))}",
        f"downloadURL = {to_toml_scalar(cfg.get('downloadURL', ''))}",
        "",
    ]
    lines.extend(toml_path_lines(cfg.get("path", [])))
    lines.append("")

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

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Construct pkg.toml from legacy files in current dir")
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
        temp = out_path.with_suffix(out_path.suffix + ".preview")
        write_pkg_toml(temp, cfg)
        print(temp.read_text(encoding="utf-8"))
        temp.unlink(missing_ok=True)
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
