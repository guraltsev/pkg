"""Install Mogan STEM from its nested portable release archive.

Mogan's Windows release asset is a ZIP bundle produced by Velopack. Its
portable application archive is nested inside that bundle, so this module
extracts the portable archive into the staged application directory without
running the installer.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a selected
release. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The module streams the versioned portable ZIP to temporary storage, validates
every contained path, and copies its files into the manager-owned staging
directory.
"""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the selected Mogan STEM portable archive into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded release artifact, candidate,
        and staging paths.

    Raises
    ------
    RuntimeError
        The release bundle lacks its versioned portable archive or contains an
        unsafe archive path.
    zipfile.BadZipFile
        The release bundle or its portable archive is not a valid ZIP file.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])
    version = context["candidate"]["version"]
    portable_name = f"MoganSTEM-v{version}-64bit-stable-Portable.zip"

    # Materialize only the nested portable payload temporarily; the outer
    # release bundle also carries updater packages that do not belong in App.
    with tempfile.TemporaryDirectory(prefix="gupkg-mogan-") as temporary_root:
        portable_archive = Path(temporary_root) / portable_name
        with zipfile.ZipFile(artifact) as release_archive:
            try:
                with release_archive.open(portable_name) as source:
                    with portable_archive.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
            except KeyError as exc:
                raise RuntimeError(
                    f"Mogan release bundle does not contain {portable_name}"
                ) from exc

        # Populate only safe relative paths so a malicious release cannot
        # write outside the manager-owned staging directory during extraction.
        stage_app.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(portable_archive) as archive:
            for member in archive.infolist():
                _extract_member(archive, member, stage_app)

    # The portable marker changes Mogan's runtime behavior. The package
    # manager already provides an isolated application directory, so remove it
    # before activation and use Mogan's standard data-location behavior.
    (stage_app / ".portable").unlink(missing_ok=True)


def _extract_member(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path
) -> None:
    """Copy one safe ZIP member into the staged application directory."""
    relative_path = PurePosixPath(member.filename.replace("\\", "/"))
    is_symlink = stat.S_ISLNK(member.external_attr >> 16)
    if relative_path.is_absolute() or ".." in relative_path.parts or is_symlink:
        raise RuntimeError(f"Mogan portable archive contains unsafe path: {member.filename}")

    output_path = destination.joinpath(*relative_path.parts)
    if member.is_dir():
        output_path.mkdir(parents=True, exist_ok=True)
        return

    # Create each parent before streaming the member to preserve the archive's
    # layout without loading large runtime files into memory.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source:
        with output_path.open("wb") as target:
            shutil.copyfileobj(source, target)
