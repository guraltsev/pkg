"""Discover the latest qBittorrent 64-bit Windows installer.

SourceForge's release metadata identifies the current Windows installer. The
checker uses that metadata to obtain the release version and constructs a
stable download URL for the package update workflow.

Usage and API
-------------
The package manager calls ``check_update(context)`` during an update check.
The returned candidate describes a newer 64-bit Windows installer, if one is
available.

Implementation Approach
-----------------------
The checker reads SourceForge's platform-specific release metadata, accepts one
exact ``qbittorrent_<version>_x64_setup.exe`` filename, and compares dotted
numeric versions before exposing the candidate.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

# SourceForge's metadata endpoint exposes the installer for each supported
# platform, while the stable file URL resolves the selected installer at use.
_RELEASE_METADATA = "https://sourceforge.net/projects/qbittorrent/best_release.json"
_DOWNLOAD_ROOT = "https://sourceforge.net/projects/qbittorrent/files"
_INSTALLER_NAME = re.compile(
    r"\bqbittorrent_(?P<version>\d+(?:\.\d+)*)_x64_setup\.exe\b",
    re.IGNORECASE,
)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the latest qBittorrent installer when it is newer.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the currently installed package version.

    Returns
    -------
    dict[str, str] | None
        Candidate installer metadata, or ``None`` when the installed version is
        current or newer.

    Raises
    ------
    RuntimeError
        The release metadata cannot be read or does not identify exactly one
        supported installer.
    """
    # Read the platform metadata rather than the generic latest-download URL.
    # That URL now redirects to a source archive, whose binary payload cannot
    # identify the Windows installer this package needs.
    request = urllib.request.Request(
        _RELEASE_METADATA, headers={"User-Agent": "gupkg-qbittorrent-update-check/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            metadata = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read the qBittorrent release metadata: {exc}"
        ) from exc

    # Select Windows explicitly because the generic SourceForge latest endpoint
    # chooses the cross-platform source archive instead of this installer.
    try:
        filename_path = metadata["platform_releases"]["windows"]["filename"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "qBittorrent release metadata does not identify a Windows installer"
        ) from exc

    # One exact installer filename makes the version and platform selection
    # unambiguous even if SourceForge adds surrounding directory information.
    matches = list(dict.fromkeys(_INSTALLER_NAME.findall(str(filename_path))))
    if len(matches) != 1:
        raise RuntimeError(
            "qBittorrent release metadata must identify exactly one 64-bit installer"
        )
    version = matches[0]
    current_version = context.get("current", {}).get("version")
    if (
        isinstance(current_version, str)
        and current_version != "bootstrap"
        and _compare_versions(version, current_version) <= 0
    ):
        return None

    filename = f"qbittorrent_{version}_x64_setup.exe"
    return {
        "candidateId": f"qbittorrent:{version}:{filename}",
        "version": version,
        "url": f"{_DOWNLOAD_ROOT}{filename_path}/download",
        "fileName": filename,
    }


def _compare_versions(left: str, right: str) -> int:
    """Compare dotted numeric qBittorrent release versions."""
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)
