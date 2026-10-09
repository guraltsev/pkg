"""Create, repair, or convert a package's ``pkg.toml`` safely.

``fix_package_config(...)`` selects exactly one repair mode for a directory:
synchronize the directory-owned metadata of an existing ``pkg.toml`` (optionally
importing ``.lnk`` shortcuts), convert recognized legacy JSON metadata, or write
a documented starter file. The complete replacement is rendered and validated
first; only then is a timestamped backup made and the file replaced atomically,
so a failure never leaves a damaged or half-written configuration.

Usage and API
-------------
Call ``fix_package_config(directory, identity, ...)`` with a directory resolved
by :func:`gupkg.commands.resolve_package`; ``has_legacy_metadata(...)`` tells
whether a directory holds convertible legacy files.

Implementation Approach
-----------------------
Mode selection, rendering, validation, backup, and write are separate steps in
that order. Imported shortcut files are archived only after the replacement
succeeded.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .configuration import normalize_runtime_config, validate_runtime_config
from .core import (
    EXIT_MUTATION_ERROR,
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    write_text_atomic,
)
from .legacy_to_gupkg_toml import (
    build_config,
    pick_all_matching,
    pick_legacy_metadata_files,
    render_gupkg_toml,
)
from .metadata import create_starter_config, sync_config_metadata_text
from .outcome import Outcome, failure


def has_legacy_metadata(directory: Path) -> bool:
    """Return whether *directory* holds recognized legacy package metadata files."""
    return bool(pick_legacy_metadata_files(directory)) or any(
        pick_all_matching(directory, prefix)
        for prefix in ("environment", "env", "shortcut", "path", "bin")
    )


def fix_package_config(
    directory: Path,
    identity: PackageIdentity | None,
    *,
    backup: bool = True,
    import_shortcuts: bool = True,
    output: Path | None = None,
) -> Outcome:
    """Synchronize, convert, or create ``pkg.toml`` for one directory.

    Parameters
    ----------
    directory : Path
        Version directory, or a legacy package directory.
    identity : PackageIdentity or None
        Directory-derived identity; ``None`` for a legacy directory without a
        valid version name.
    backup : bool, default=True
        Keep a timestamped ``pkg.toml.bak.*`` copy of a replaced file.
    import_shortcuts : bool, default=True
        Import ``.lnk`` files from ``_shortcuts`` while synchronizing.
    output : Path, optional
        Destination for a legacy conversion only.

    Returns
    -------
    Outcome
        ``fixed``, ``unchanged``, or a failure whose exit code is ``2`` for
        invalid input and ``3`` when the file system could not be written.
    """
    # Select exactly one repair mode before any work, and reject options that
    # do not apply to it.
    destination = directory / "pkg.toml"
    if destination.exists():
        operation = "synchronize"
    elif has_legacy_metadata(directory):
        operation = "legacy-conversion"
    else:
        operation = "starter"
    if output is not None and operation != "legacy-conversion":
        return failure("config-fix", "--output is valid only when converting legacy metadata")
    if not import_shortcuts and operation != "synchronize":
        return failure("config-fix", "--import-shortcuts applies only to current canonical metadata")
    if operation != "legacy-conversion" and identity is None:
        return failure("config-fix", "Canonical metadata requires a valid version directory")

    warnings: list[str] = []
    imported_shortcuts = False
    try:
        # Render the complete replacement document for the selected mode.
        if operation == "legacy-conversion":
            if output is not None:
                destination = Path(output).expanduser().resolve()
            # The converter reports best-effort field diagnostics on stdout;
            # capture them so they become warnings in every front end.
            converter_output = io.StringIO()
            with contextlib.redirect_stdout(converter_output):
                rendered = render_gupkg_toml(build_config(directory))
            warnings = [line.strip() for line in converter_output.getvalue().splitlines() if line.strip()]
        elif operation == "synchronize":
            rendered, _ = sync_config_metadata_text(destination.read_text(encoding="utf-8"), identity)
            shortcuts_dir = directory / "_shortcuts"
            if import_shortcuts and shortcuts_dir.is_dir():
                from .shortcuts_to_gupkg_toml import (
                    package_path_context,
                    read_shortcut_directory,
                    replace_shortcut_tables,
                )

                imported = read_shortcut_directory(shortcuts_dir, package_path_context(directory))
                if imported:
                    rendered = replace_shortcut_tables(rendered, imported)
                    imported_shortcuts = True
        else:
            rendered = create_starter_config(identity)

        # Validate the whole replacement, including preserved unrelated
        # fields, before creating a backup or touching the destination.
        _validate_replacement(rendered, identity, legacy=operation == "legacy-conversion")
        data = {"config": {"path": str(destination.resolve()), "operation": operation}}
        if destination.exists() and destination.read_text(encoding="utf-8") == rendered:
            return Outcome("config-fix", ActionResult(ok=True, warnings=warnings, status="unchanged"), data)

        # Back up, replace atomically, and consume imported shortcuts only
        # after the replacement succeeded.
        backup_path = None
        if backup and destination.exists():
            backup_path = _backup_name(destination)
            shutil.copy2(destination, backup_path)
        write_text_atomic(destination, rendered)
        if imported_shortcuts:
            from .shortcuts_to_gupkg_toml import archive_imported_shortcuts

            archive_imported_shortcuts(directory / "_shortcuts")
        data["config"]["backup_path"] = str(backup_path.resolve()) if backup_path else None
        return Outcome("config-fix", ActionResult(ok=True, changed=True, warnings=warnings, status="fixed"), data)
    except (ConfigValidationError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        return failure("config-fix", str(exc))
    except OSError as exc:
        return failure("config-fix", str(exc), EXIT_MUTATION_ERROR)


def _validate_replacement(rendered: str, identity: PackageIdentity | None, *, legacy: bool) -> None:
    """Require a replacement ``pkg.toml`` to be one valid current package document.

    Within a version directory the full runtime schema is validated. A legacy
    conversion must additionally carry well-typed package metadata, since the
    converter infers it best-effort.
    """
    parsed = tomllib.loads(rendered)
    if identity is not None:
        validate_runtime_config(normalize_runtime_config(parsed, identity))
    if not legacy:
        return
    expected_types = {"name": str, "version": str, "localVersion": int, "only_portable": bool}
    for key, expected in expected_types.items():
        value = parsed.get(key)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigValidationError(
                "Legacy metadata did not produce one unambiguous current package document: "
                f"missing or invalid {key}"
            )


def _backup_name(destination: Path) -> Path:
    """Choose an unused ``<name>.bak.<UTC timestamp>[.N]`` sibling backup path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = destination.with_name(f"{destination.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.bak.{stamp}.{suffix}")
        suffix += 1
    return candidate
