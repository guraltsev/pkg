"""Discover the newest System Informer binary ZIP release.

System Informer release tags include a fourth build component that its ZIP
filenames omit. This hook reads GitHub's latest-release metadata and selects
the matching binary archive without conflating those two version formats.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_RELEASE_URL = "https://api.github.com/repos/winsiderss/systeminformer/releases/latest"
_TAG = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+(?:\.\d+)*)$", re.IGNORECASE)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the latest System Informer ZIP when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Package-manager context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata for the binary archive, or ``None`` when the
        current release has a healthy payload.

    Raises
    ------
    RuntimeError
        The GitHub response lacks a valid release tag or its matching binary
        ZIP asset.
    """
    # Read the publisher's stable-release record and retain GitHub's asset
    # URLs rather than constructing a URL from an undocumented convention.
    request = urllib.request.Request(
        _RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gupkg-systeminformer-update-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the System Informer release: {exc}") from exc

    # Preserve the complete tag as the package version, while deriving the
    # ZIP's three-component version separately from its first three parts.
    if not isinstance(release, dict):
        raise RuntimeError("System Informer release response must be an object")
    tag = release.get("tag_name")
    release_id = release.get("id")
    match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None or not isinstance(release_id, int):
        raise RuntimeError("System Informer release has no valid tag or release ID")
    version = match.group("version")
    archive_version = ".".join(version.split(".")[:3])
    filename = f"systeminformer-{archive_version}-release-bin.zip"

    # A healthy installation of the same upstream tag requires no update.
    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    # Require one exact uploaded asset so an unrelated setup executable or a
    # malformed release cannot become the portable application payload.
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("System Informer release has no valid asset list")
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("state") == "uploaded"
        and asset.get("name") == filename
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "System Informer release must contain exactly one uploaded asset named "
            f"{filename!r}; found {len(matches)}"
        )
    url = matches[0].get("browser_download_url")
    if not isinstance(url, str):
        raise RuntimeError("System Informer binary ZIP has no download URL")

    return {
        "candidateId": f"systeminformer:{release_id}",
        "version": version,
        "url": url,
        "fileName": filename,
    }
