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
        out[canon] = str(v)

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
            content = str(v)
    if not name or content == "":
        return {}
    return {"name": name, "content": content}


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

    for shortcut_file in pick_all_matching(base_dir, "shortcut"):
        data = read_json(shortcut_file)
        if not data:
            continue
        rows = data.get("shortcut", [])
        if not isinstance(rows, list):
            print(f"Warning: {shortcut_file.name}: 'shortcut' should be a list")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            norm = normalize_shortcut(row)
            if norm:
                out["shortcut"].append(norm)

    for bin_file in pick_all_matching(base_dir, "bin"):
        data = read_json(bin_file)
        if not data:
            continue
        rows = data.get("bin", [])
        if not isinstance(rows, list):
            print(f"Warning: {bin_file.name}: 'bin' should be a list")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            norm = normalize_bin(row)
            if norm:
                out["bin"].append(norm)

    return out


def write_pkg_toml(path: Path, cfg: dict[str, Any]) -> None:
    lines = [
        "# Auto-generated from legacy config files.",
        "# Please review and edit as needed.",
        f"name = {to_toml_scalar(cfg.get('name', ''))}",
        f"version = {to_toml_scalar(cfg.get('version', ''))}",
        f"localVersion = {to_toml_scalar(cfg.get('localVersion', ''))}",
        f"only_portable = {to_toml_scalar(cfg.get('only_portable', False))}",
        f"description = {to_toml_scalar(cfg.get('description', ''))}",
        f"homepage = {to_toml_scalar(cfg.get('homepage', ''))}",
        f"downloadURL = {to_toml_scalar(cfg.get('downloadURL', ''))}",
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

    write_pkg_toml(out_path, cfg)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
