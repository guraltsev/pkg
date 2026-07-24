"""Discover the newest stable Visual Studio Code archive for Windows.

The package manager calls ``check_update(...)`` to read Microsoft's ordered
stable-release JSON array and construct the corresponding Windows x64 archive
URL. Downloading and ZIP extraction remain the package manager's responsibility.

Usage and API
-------------
The package manager loads this trusted hook through ``[update.check]`` and
calls ``check_update(context)`` with the current package identity.

Implementation Approach
-----------------------
The hook validates the first release identifier returned by the official
stable channel before interpolating it into Microsoft's archive endpoint.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


# The API returns stable versions newest-first as a JSON string array.
RELEASES_URL = "https://update.code.visualstudio.com/api/releases/stable"

# The version component is validated before interpolation into this endpoint.
DOWNLOAD_URL = (
    "https://update.code.visualstudio.com/"
    "{version}/win32-x64-archive/stable"
)

PKG_MODULE_API = 1


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable Windows x64 archive when an update is available.

    Parameters
    ----------
    context : dict[str, Any]
        Package-manager context containing the current version and selected
        update channel.

    Returns
    -------
    dict[str, str] | None
        Normalized candidate metadata for the newest stable archive, or
        ``None`` when the installed version is already current.

    Raises
    ------
    RuntimeError
        The release API response is empty, malformed, or lacks a supported
        stable version identifier.
    """
    if context.get("channel") != "stable":
        raise RuntimeError("VS Code update hook supports only the stable channel")

    # Fetch the small release index; the manager separately downloads and
    # extracts the selected archive after candidate validation.
    request = urllib.request.Request(
        RELEASES_URL,
        headers={"User-Agent": "pkg-vscode-update-check/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read VS Code stable releases: {exc}") from exc

    # Microsoft orders this endpoint newest-first. Restrict interpolation to
    # ordinary stable semantic versions such as 1.130.0.
    if not isinstance(releases, list) or not releases:
        raise RuntimeError("VS Code stable release API returned no versions")
    latest = releases[0]
    if not isinstance(latest, str) or re.fullmatch(r"\d+(?:\.\d+)+", latest) is None:
        raise RuntimeError("VS Code stable release API returned an invalid version")

    if context.get("current", {}).get("version") == latest:
        return None

    return {
        "candidateId": f"vscode-stable:{latest}",
        "version": latest,
        "url": DOWNLOAD_URL.format(version=latest),
        "fileName": f"VSCode-win32-x64-{latest}.zip",
    }
