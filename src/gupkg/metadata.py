"""Create and synchronize package-owned ``pkg.toml`` metadata.

The package directory owns the canonical name, version, local revision, and
portability fields. Synchronization updates only those top-level assignments,
preserving unrelated tables, comments, ordering, and quoted ``#`` characters.

Usage and API
-------------
Call ``sync_config_metadata_text(...)`` to synchronize existing TOML text and
``create_starter_config(...)`` to render a documented starter document. Both
return text; ``gupkg config-fix`` owns validation, backups, and writing.

Implementation Approach
-----------------------
A line-aware parser identifies editable top-level metadata assignments while
tracking TOML strings and comments. The rendered document is parsed again
before it is returned, so a rewrite can never produce invalid TOML.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any, Dict, Tuple

from .configuration import validate_top_level_keys
from .core import ConfigValidationError, PackageIdentity


def _to_toml_scalar(value: Any) -> str:
    """Render a Python value as TOML literal text.

    Parameters
    ----------
    value : Any
        Python scalar or list value.

    Returns
    -------
    str
        TOML literal text representing *value*.

    """
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_to_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def metadata_sync_payload(identity: PackageIdentity) -> Dict[str, Any]:
    """Return the directory-derived metadata owned by ``gupkg``.

    Parameters
    ----------
    identity : PackageIdentity
        Package identity whose metadata should be serialized.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing only metadata fields that ``gupkg`` owns.

    """
    return {
        "name": identity.name,
        "version": identity.version,
        "localVersion": identity.local_version,
        "only_portable": identity.only_portable_by_name,
    }


def _parse_editable_top_level_metadata_line(line: str) -> Tuple[str, str, str, str]:
    """Parse one editable top-level metadata line.

    The helper is intentionally narrow: it supports the single-line assignment
    shape that ``config-fix`` rewrites safely and understands ``#`` only when
    it appears outside quoted strings.
    """
    index = 0
    while index < len(line) and line[index].isspace():
        index += 1
    indent = line[:index]

    key_start = index
    if key_start >= len(line) or not (
        line[key_start].isalpha() or line[key_start] == "_"
    ):
        raise ConfigValidationError(
            "pkg.toml contains a metadata line that config-fix cannot rewrite safely. Edit the line manually."
        )
    index += 1
    while index < len(line) and (line[index].isalnum() or line[index] == "_"):
        index += 1
    key = line[key_start:index]

    while index < len(line) and line[index] in " \t":
        index += 1
    if index >= len(line) or line[index] != "=":
        raise ConfigValidationError(
            f"pkg.toml metadata line for '{key}' cannot be updated safely. Edit the line manually."
        )
    index += 1
    while index < len(line) and line[index] in " \t":
        index += 1
    value_start = index

    in_basic_string = False
    in_literal_string = False
    escaped = False

    while index < len(line):
        char = line[index]
        if in_basic_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_basic_string = False
            index += 1
            continue

        if in_literal_string:
            if char == "'":
                in_literal_string = False
            index += 1
            continue

        if char == "#":
            break
        if char == '"':
            if line.startswith('"""', index):
                raise ConfigValidationError(
                    f"pkg.toml metadata line for '{key}' cannot be updated safely because multi-line strings are not supported here. Edit the line manually."
                )
            in_basic_string = True
        elif char == "'":
            if line.startswith("'''", index):
                raise ConfigValidationError(
                    f"pkg.toml metadata line for '{key}' cannot be updated safely because multi-line strings are not supported here. Edit the line manually."
                )
            in_literal_string = True
        index += 1

    if in_basic_string or in_literal_string or escaped:
        raise ConfigValidationError(
            f"pkg.toml metadata line for '{key}' cannot be updated safely because the value is not a supported single-line scalar. Edit the line manually."
        )

    raw_value = line[value_start:index]
    value_text = raw_value.rstrip(" \t")
    comment_text = raw_value[len(value_text) :] + line[index:]
    if value_text == "":
        raise ConfigValidationError(
            f"pkg.toml metadata line for '{key}' is missing a value. Edit the line manually."
        )

    return indent, key, value_text, comment_text


def sync_config_metadata_text(text: str, identity: PackageIdentity) -> Tuple[str, bool]:
    """Synchronize owned metadata directly in canonical ``pkg.toml`` text.

    Parameters
    ----------
    text : str
        Existing TOML text to update.
    identity : PackageIdentity
        Package identity that supplies the target metadata values.

    Returns
    -------
    Tuple[str, bool]
        Tuple ``(rendered_text, changed)``.

    Raises
    ------
    ConfigValidationError
        If the existing file is not valid TOML, uses unknown or legacy
        top-level keys, or has a metadata line that cannot be rewritten safely.

    """
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigValidationError(
            f"pkg.toml is structurally invalid and cannot be updated safely: {exc}. Edit the config manually."
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigValidationError("pkg.toml must contain a top-level TOML table.")

    validate_top_level_keys(parsed)

    metadata = metadata_sync_payload(identity)
    key_pattern = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

    # Locate editable top-level metadata assignments. Everything from the
    # first table header onward belongs to a table and is never rewritten.
    lines = text.splitlines(keepends=True)
    first_table: int | None = None
    line_indexes: Dict[str, int] = {}
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            first_table = index
            break
        match = key_pattern.match(line)
        if match is None or match.group("key") not in metadata:
            continue
        key = match.group("key")
        _parse_editable_top_level_metadata_line(line.rstrip("\r\n"))
        if key in line_indexes:
            raise ConfigValidationError(f"Duplicate metadata key '{key}' in config.")
        line_indexes[key] = index

    # Rewrite present assignments in place, keeping indentation, trailing
    # comments, and the line's own newline style.
    changed = False
    for key, index in line_indexes.items():
        line = lines[index]
        ending = "\r\n" if line.endswith("\r\n") else "\n"
        indent, _, existing_value, comment_text = _parse_editable_top_level_metadata_line(
            line.rstrip("\r\n")
        )
        rendered_value = _to_toml_scalar(metadata[key])
        if existing_value != rendered_value:
            lines[index] = f"{indent}{key} = {rendered_value}{comment_text}{ending}"
            changed = True

    # Insert missing keys after the existing metadata block, otherwise before
    # the first table, otherwise at the end of a table-free document.
    missing = [key for key in metadata if key not in line_indexes]
    if missing:
        if line_indexes:
            insert_at = max(line_indexes.values()) + 1
        elif first_table is not None:
            insert_at = first_table
        else:
            if lines and lines[-1].strip() != "":
                lines.append("\n")
            insert_at = len(lines)
        if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
            lines[insert_at - 1] += "\n"
        lines[insert_at:insert_at] = [
            f"{key} = {_to_toml_scalar(metadata[key])}\n" for key in missing
        ]
        changed = True

    rendered = "".join(lines)
    try:
        reparsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigValidationError(
            f"pkg.toml metadata rewrite produced invalid TOML and was aborted: {exc}. Edit the config manually."
        ) from exc
    if not isinstance(reparsed, dict):
        raise ConfigValidationError(
            "pkg.toml metadata rewrite produced an invalid top-level structure."
        )

    return rendered, changed


def create_starter_config(identity: PackageIdentity) -> str:
    """Create a documented starter ``pkg.toml`` for a package.

    Parameters
    ----------
    identity : PackageIdentity
        Package identity that supplies starter metadata values.

    Returns
    -------
    str
        TOML text that includes synchronized metadata, documentation comments,
        and commented example blocks for the supported runtime sections.

    """
    metadata = metadata_sync_payload(identity)

    example_exe = re.sub(r"[^A-Za-z0-9._-]+", "", identity.name) or "App"
    example_env = (
        re.sub(r"[^A-Za-z0-9]+", "_", identity.name).strip("_").upper() or "APP"
    )
    example_wrapper = (
        re.sub(r"[^A-Za-z0-9]+", "-", identity.name).strip("-").lower() or "app"
    )
    example_description = ""
    example_homepage = "https://"
    example_download = "https://"
    example_exe_path = rf"$App\{example_exe}.exe"
    example_icon_path = rf"$Icons\{example_exe}.ico,0"
    example_env_value = rf"${{USERPROFILE}}\{identity.name}"
    example_wrapper_name = example_wrapper

    lines = [
        "# Generated automatically by `gupkg`.",
        "",
        "# `gupkg config-fix` keeps these fields aligned with the package folder name.",
        f"name = {_to_toml_scalar(metadata['name'])}",
        f"version = {_to_toml_scalar(metadata['version'])}",
        f"localVersion = {_to_toml_scalar(metadata['localVersion'])}",
        "# Set only_portable = true when the package stores user config in its folder",
        "# and therefore must only be installed portably.",
        f"only_portable = {_to_toml_scalar(metadata['only_portable'])}",
        "",
        f"# description = {_to_toml_scalar(example_description)}",
        f"# homepage = {_to_toml_scalar(example_homepage)}",
        "# [origin]",
        f"# url = {_to_toml_scalar(example_download)}",
        '# checksum = "sha256:<64 hex characters>"',
        '# extractSubdir = "tool-portable"',
        "#",
        "# Or replace url/checksum/extractSubdir with a package-local script:",
        '# script = "scripts/populate-app.ps1"',
        "#",
        "# Or clone a Git ref directly into App:",
        '# mode = "git"',
        '# url = "git@github.com:owner/repository.git"',
        '# ref = "refs/heads/main"  # default',
        "#",
        "# Multiple origin versions:",
        "#   Use repeated [[origin.versions]] tables to keep older upstream",
        "#   payload locations in the same pkg.toml. Each entry must have a",
        "#   unique version. url/script can be filled in later, but installing",
        "#   from that version requires one of them.",
        "#",
        "# Current origin can stay inline above, while old versions live here:",
        "# [[origin.versions]]",
        '# version = "1.0.0"',
        '# url = "https://example.invalid/tool-1.0.0.zip"',
        '# checksum = "sha256:<64 hex characters>"',
        '# extractSubdir = "tool-1.0.0"',
        "#",
        "# [[origin.versions]]",
        '# version = "0.9.0"',
        '# url = "https://example.invalid/tool-0.9.0.zip"',
        '# extractSubdir = "tool-0.9.0"',
        "#",
        "# Or use the package top-level version to select the current origin:",
        "#   1. omit top-level url/script above",
        "#   2. define a [[origin.versions]] entry whose version matches",
        "#      the top-level package version above",
        "# [origin]",
        "#",
        "# [[origin.versions]]",
        f"# version = {_to_toml_scalar(metadata['version'])}",
        '# url = "https://example.invalid/tool-current.zip"',
        "",
        "# Update examples:",
        "# Use `gupkg update --check-only`, `gupkg update --download-only`, or",
        "# `gupkg update` to update explicitly.",
        "#",
        "# Git origin workflow (App is a Git work tree). The update check",
        "# inherits Git mode and ref from [origin]. The ordinary git payload",
        "# creates a new timestamped version:",
        "# [update.payload]",
        '# mode = "git"',
        "#",
        "# Downloaded zip workflow (pkg.local/check_update.py finds releases):",
        "# [update.check]",
        '# mode = "module"',
        '# channel = "stable"',
        "#",
        "# [update.payload]",
        '# mode = "zip"',
        '# extractSubdir = "tool-portable"',
        "# ignore_checksum = false",
        "#",
        '# For another archive layout, use payload.mode = "module" and provide',
        "# pkg.local/unpack_app.py. Package-local update modules are trusted in-process code.",
        "",
        "# Variable expansion reference:",
        "#   $App, $Icons, $Shortcuts -> package directories under <package>/current/",
        "#   ${VAR} -> environment variable expansion and must resolve",
        "#   plain non-package $NAME -> unresolved in regular fields, literal in [[bin]] content",
        "#   $$ -> literal $",
        "",
        "# Examples:",
        "# [[shortcut]]",
        f"# name = {_to_toml_scalar(identity.name)}",
        f"# targetPath = {_to_toml_scalar(example_exe_path)}",
        '# arguments = "--example"',
        '# workingDirectory = "$App"',
        f"# iconLocation = {_to_toml_scalar(example_icon_path)}",
        f"# description = {_to_toml_scalar(identity.name)}",
        "",
        "# Example environment variable available after installation.",
        "# [[environment]]",
        f"# Name = {_to_toml_scalar(f'{example_env}_HOME')}",
        f"# Value = {_to_toml_scalar(example_env_value)}",
        "",
        "# Example PATH entry.",
        "# [[path]]",
        '# value = "$App"',
        "",
        "# Example native executable shim placed in the scope bin directory.",
        "# [[bin]]",
        f"# name = {_to_toml_scalar(example_wrapper_name)}",
        f"# target = {_to_toml_scalar(example_exe_path)}",
        '# arguments = ["--example"]',
        '# type = "console"  # or "gui"',
        "# forward_args = true  # default",
    ]
    return "\n".join(lines).rstrip() + "\n"
