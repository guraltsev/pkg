"""Discover the latest qBittorrent 64-bit Windows installer.

SourceForge's stable latest-download page contains the selected filename in its
HTML response. The checker uses that filename to obtain the release version
and returns the same canonical download URL for the package update workflow.

Usage and API
-------------
The package manager calls ``check_update(context)`` during an update check.
The returned candidate describes a newer 64-bit Windows installer, if one is
available.

Implementation Approach
-----------------------
The checker reads the latest-download response and accepts one exact
``qbittorrent_<version>_x64_setup.exe`` filename. It compares dotted numeric
versions before exposing the candidate.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_LATEST_DOWNLOAD = "https://sourceforge.net/projects/qbittorrent/files/latest/download"
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
        The latest-download page cannot be read or does not identify exactly
        one supported installer.
    """
    # Read the stable download endpoint because SourceForge publishes the
    # selected installer filename in its response without requiring browser JS.
    request = urllib.request.Request(
        _LATEST_DOWNLOAD, headers={"User-Agent": "pkg-qbittorrent-update-check/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the qBittorrent download page: {exc}") from exc

    # One exact installer filename makes the version and platform selection
    # unambiguous even when the page contains unrelated project links.
    matches = list(dict.fromkeys(_INSTALLER_NAME.findall(page)))
    if len(matches) != 1:
        raise RuntimeError(
            "qBittorrent latest download must identify exactly one 64-bit installer"
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
        "url": _LATEST_DOWNLOAD,
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
