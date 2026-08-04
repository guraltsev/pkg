#!/usr/bin/env python3
"""Import Windows shortcut files from ``_shortcuts`` into ``pkg.toml``.

Call ``import_shortcuts(...)`` or the command-line ``main()`` wrapper to read
real ``.lnk`` files from one package version directory and rewrite matching
``[[shortcut]]`` entries in the existing TOML file. Package-owned absolute
paths are converted back to ``$App``, ``$Icons``, and ``$Shortcuts`` while
unrelated configuration sections and comments are preserved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ------------------------------------------
# Section: Command workflow
# ------------------------------------------
#
# The public command stays small: resolve package paths, read shortcuts, rewrite
# matching TOML tables, and either print or persist the result.


@dataclass(frozen=True)
class ShortcutEntry:
    """Represent one canonical ``[[shortcut]]`` table.

    Attributes
    ----------
    name : str
        Shortcut name relative to the package shortcut root, without ``.lnk``.
    targetPath : str
        Executable or document target path.
    arguments : str
        Command-line arguments stored in the shortcut.
    workingDirectory : str
        Working directory stored in the shortcut.
    iconLocation : str
        Icon location stored in the shortcut.
    description : str
        Description stored in the shortcut.
    """

    name: str
    targetPath: str
    arguments: str = ""
    workingDirectory: str = ""
    iconLocation: str = ""
    description: str = ""


def import_shortcuts(
    base_dir: Path,
    *,
    shortcuts_dir_name: str = "_shortcuts",
    config_name: str = "pkg.toml",
) -> tuple[str, list[ShortcutEntry]]:
    """Render updated ``pkg.toml`` text after importing shortcut files.

    Parameters
    ----------
    base_dir : Path
        Package version directory containing ``pkg.toml`` and ``_shortcuts``.
    shortcuts_dir_name : str
        Shortcut source directory name or path, relative to ``base_dir`` unless
        absolute.
    config_name : str
        TOML config filename or path, relative to ``base_dir`` unless absolute.

    Returns
    -------
    tuple[str, list[ShortcutEntry]]
        Updated TOML text and the shortcut entries imported from disk.

    Raises
    ------
    FileNotFoundError
        If ``pkg.toml`` or the shortcuts directory does not exist.
    RuntimeError
        If the existing TOML is invalid or no shortcut files can be read.
    """

    base_dir = base_dir.absolute()
    config_path = Path(config_name)
    if not config_path.is_absolute():
        config_path = base_dir / config_path
    shortcuts_dir = Path(shortcuts_dir_name)
    if not shortcuts_dir.is_absolute():
        shortcuts_dir = base_dir / shortcuts_dir

    if not config_path.exists():
        raise FileNotFoundError(f"pkg.toml not found: {config_path}")
    if not shortcuts_dir.is_dir():
        raise FileNotFoundError(f"shortcuts directory not found: {shortcuts_dir}")

    original_text = config_path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(original_text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"pkg.toml is invalid and cannot be updated safely: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("pkg.toml must contain a top-level TOML table.")

    # Read the shortcut files before editing TOML so filesystem or COM failures
    # do not leave a partially updated config.
    path_context = package_path_context(base_dir)
    shortcuts = read_shortcut_directory(shortcuts_dir, path_context)
    if not shortcuts:
        raise RuntimeError(f"no .lnk files found under {shortcuts_dir}")

    rendered = replace_shortcut_tables(original_text, shortcuts)
    return rendered, shortcuts


def main() -> int:
    """Run the shortcut import helper CLI.

    Returns
    -------
    int
        Process exit code.
    """

    parser = argparse.ArgumentParser(
        description="Import .lnk files from _shortcuts into pkg.toml"
    )
    parser.add_argument(
        "--dir",
        default=".",
        help="Package version directory (default: current directory)",
    )
    parser.add_argument(
        "--shortcuts-dir",
        default="_shortcuts",
        help="Shortcut directory (default: ./_shortcuts)",
    )
    parser.add_argument(
        "--config", default="pkg.toml", help="TOML config path (default: ./pkg.toml)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print updated TOML instead of writing"
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not write pkg.toml.bak before replacing pkg.toml",
    )
    args = parser.parse_args()

    try:
        base_dir = Path(args.dir).absolute()
        rendered, shortcuts = import_shortcuts(
            base_dir,
            shortcuts_dir_name=args.shortcuts_dir,
            config_name=args.config,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / config_path

    # Let callers inspect the exact rewritten TOML before any file mutation.
    if args.dry_run:
        print(rendered, end="")
        return 0

    # Preserve the prior config by default because shortcut import rewrites one
    # section in place and users may want an easy rollback.
    if not args.no_backup:
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
    config_path.write_text(rendered, encoding="utf-8")
    print(f"Wrote {config_path} ({len(shortcuts)} shortcut(s))")
    return 0


# ------------------------------------------
# Section: Shortcut reading
# ------------------------------------------
#
# Python's standard library cannot parse Shell Link files. On Windows, WScript
# exposes the fields that ``gupkg`` needs and keeps this helper dependency-free.


def read_shortcut_directory(
    shortcuts_dir: Path, path_context: list[tuple[Path, str]]
) -> list[ShortcutEntry]:
    """Read every ``.lnk`` file below a shortcut directory."""

    shortcut_files = sorted(
        path for path in shortcuts_dir.rglob("*.lnk") if path.is_file()
    )
    entries: list[ShortcutEntry] = []
    for shortcut_file in shortcut_files:
        raw = read_windows_shortcut(shortcut_file)

        # The Start Menu name comes from placement under ``_shortcuts``, not
        # from mutable fields inside the Shell Link object.
        relative_name = shortcut_file.relative_to(shortcuts_dir).with_suffix("")
        name = str(relative_name).replace("/", "\\")

        entries.append(
            ShortcutEntry(
                name=name,
                targetPath=normalize_package_path(
                    str(raw.get("TargetPath", "")), path_context
                ),
                arguments=str(raw.get("Arguments", "")),
                workingDirectory=normalize_package_path(
                    str(raw.get("WorkingDirectory", "")), path_context
                ),
                iconLocation=normalize_icon_location(
                    str(raw.get("IconLocation", "")), path_context
                ),
                description=str(raw.get("Description", "")),
            )
        )
    return entries


def read_windows_shortcut(shortcut_path: Path) -> dict[str, str]:
    """Read shortcut fields with Windows Script Host."""

    if os.name != "nt":
        raise RuntimeError("reading .lnk files requires Windows")

    script = r"""
$ErrorActionPreference = 'Stop'
$inputObject = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut([string]$inputObject.ShortcutPath)
[pscustomobject]@{
  TargetPath = [string]$shortcut.TargetPath
  Arguments = [string]$shortcut.Arguments
  WorkingDirectory = [string]$shortcut.WorkingDirectory
  IconLocation = [string]$shortcut.IconLocation
  Description = [string]$shortcut.Description
} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        input=json.dumps({"ShortcutPath": str(shortcut_path)}),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_text = (
            result.stderr or result.stdout or "unknown PowerShell shortcut error"
        ).strip()
        raise RuntimeError(f"failed to read {shortcut_path}: {error_text}")
    return json.loads(result.stdout)


# ------------------------------------------
# Section: Path normalization
# ------------------------------------------
#
# The importer sees fully expanded absolute paths, current-junction paths, and
# known package-variable spellings. Convert only package-owned paths back to
# gupkg variables and leave external paths alone.


def package_path_context(base_dir: Path) -> list[tuple[Path, str]]:
    """Return package-owned path prefixes in longest-first match order."""

    package_root = base_dir.parent
    current_dir = package_root / "current"
    candidates = [
        (base_dir / "App", "$App"),
        (base_dir / "Icons", "$Icons"),
        (base_dir / "Shortcuts", "$Shortcuts"),
        (current_dir / "App", "$App"),
        (current_dir / "Icons", "$Icons"),
        (current_dir / "Shortcuts", "$Shortcuts"),
    ]
    return sorted(candidates, key=lambda item: len(str(item[0])), reverse=True)


def normalize_package_path(value: str, path_context: list[tuple[Path, str]]) -> str:
    """Convert package-owned path text to canonical package variables."""

    if value == "":
        return ""

    # Normalize old variable spellings first, then prefer the longest absolute
    # prefix so nested package folders map back to the most specific variable.
    normalized = normalize_known_package_variables(value)
    for prefix, variable in path_context:
        converted = replace_path_prefix(normalized, prefix, variable)
        if converted != normalized:
            return converted
    return normalized


def normalize_icon_location(value: str, path_context: list[tuple[Path, str]]) -> str:
    """Normalize an icon path while preserving an optional icon index suffix."""

    if value == "":
        return ""

    match = re.match(r"^(?P<path>[A-Za-z]:[\\/][^,]*)(?P<suffix>,.*)?$", value)
    if match is None:
        return normalize_package_path(value, path_context)
    return normalize_package_path(match.group("path"), path_context) + (
        match.group("suffix") or ""
    )


def replace_path_prefix(value: str, prefix: Path, variable: str) -> str:
    """Replace one absolute path prefix when it matches on a path boundary."""

    prefix_text = str(prefix.absolute())
    value_norm = value.replace("/", "\\")
    prefix_norm = prefix_text.replace("/", "\\").rstrip("\\")

    if value_norm.lower() == prefix_norm.lower():
        return variable
    if value_norm.lower().startswith(prefix_norm.lower() + "\\"):
        return variable + value_norm[len(prefix_norm) :]
    return value


def normalize_known_package_variables(value: str) -> str:
    """Normalize known package-variable spellings to canonical forms."""

    normalized = value
    component_patterns = [
        (r"\$AppPath[\\/]+App(?=[\\/]+|$)", "$App"),
        (r"\$AppPath[\\/]+Icons(?=[\\/]+|$)", "$Icons"),
        (r"\$AppPath[\\/]+Shortcuts(?=[\\/]+|$)", "$Shortcuts"),
        (r"\$(?:Gupkg_App_Path|GupkgAppPath|Package_App_Path)(?=[\\/]+|$)", "$App"),
        (r"\$(?:Gupkg_Icons_Path|GupkgIconsPath|Package_Icons_Path)(?=[\\/]+|$)", "$Icons"),
        (
            r"\$(?:Gupkg_Shortcuts_Path|GupkgShortcutsPath|Package_Shortcuts_Path)(?=[\\/]+|$)",
            "$Shortcuts",
        ),
        (r"\$AppPath(?=[\\/]+|$)", "$App"),
    ]
    for pattern, replacement in component_patterns:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


# ------------------------------------------
# Section: TOML rendering
# ------------------------------------------
#
# Existing TOML is parsed for safety, then edited textually so unrelated
# comments and sections survive. Matching shortcut tables are removed by name
# and new canonical tables are appended.


def replace_shortcut_tables(text: str, shortcuts: list[ShortcutEntry]) -> str:
    """Replace matching ``[[shortcut]]`` tables and append imported entries."""

    names = {shortcut.name for shortcut in shortcuts}
    kept_blocks: list[str] = []

    # Split the file into top-level table blocks. Only concrete ``[[shortcut]]``
    # blocks with a matching parsed name are removed.
    for block in split_toml_blocks(text):
        if is_shortcut_block(block):
            parsed_name = parse_shortcut_block_name(block)
            if parsed_name in names:
                continue
        kept_blocks.append(block)

    body = "".join(kept_blocks).rstrip()
    rendered_shortcuts = "\n\n".join(
        render_shortcut_table(shortcut).rstrip() for shortcut in shortcuts
    )
    if body:
        body += "\n\n"
    body += rendered_shortcuts
    body += "\n"

    # Verify the final TOML, including duplicate handling, before returning text
    # to the caller for either dry-run display or file replacement.
    try:
        tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"shortcut import produced invalid TOML and was aborted: {exc}"
        ) from exc

    return body


def split_toml_blocks(text: str) -> list[str]:
    """Split TOML text into table-aware blocks."""

    lines = text.splitlines(keepends=True)
    blocks: list[str] = []
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index > start and stripped.startswith("[") and not stripped.startswith("#"):
            blocks.append("".join(lines[start:index]))
            start = index
    blocks.append("".join(lines[start:]))
    return blocks


def is_shortcut_block(block: str) -> bool:
    """Return whether a TOML block starts with ``[[shortcut]]``."""

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped == "[[shortcut]]"
    return False


def parse_shortcut_block_name(block: str) -> str | None:
    """Return the shortcut name declared by one TOML block."""

    try:
        parsed = tomllib.loads(block)
    except tomllib.TOMLDecodeError:
        return None
    shortcuts = parsed.get("shortcut")
    if not isinstance(shortcuts, list) or not shortcuts:
        return None
    name = shortcuts[0].get("name")
    if not isinstance(name, str):
        return None
    return name[:-4] if name.lower().endswith(".lnk") else name


def render_shortcut_table(shortcut: ShortcutEntry) -> str:
    """Render one canonical ``[[shortcut]]`` table."""

    lines = [
        "[[shortcut]]",
        f"name = {to_toml_scalar(shortcut.name)}",
        f"targetPath = {to_toml_scalar(shortcut.targetPath)}",
    ]
    optional_fields = [
        ("arguments", shortcut.arguments),
        ("workingDirectory", shortcut.workingDirectory),
        ("iconLocation", shortcut.iconLocation),
        ("description", shortcut.description),
    ]
    for key, value in optional_fields:
        if value != "":
            lines.append(f"{key} = {to_toml_scalar(value)}")
    return "\n".join(lines) + "\n"


def to_toml_scalar(value: Any) -> str:
    """Render a Python value as TOML literal text."""

    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(to_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
