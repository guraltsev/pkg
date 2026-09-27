"""Discover the newest stable MSYS2 x86_64 self-extracting archive.

The updater reads the official MSYS2 installer release feed and selects the
newest date-tagged release containing ``msys2-base-x86_64-*.sfx.exe``. Nightly
assets and GUI installers are deliberately excluded.

Usage and API
-------------
The package manager calls ``check_update(context)`` during update discovery.
It returns the selected archive URL and its publisher-provided SHA-256 digest.

Implementation Approach
-----------------------
The release feed is validated as JSON, stable release tags are ordered by their
date values, and each candidate must expose exactly the matching SFX asset and
SHA-256 digest.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

# Only calendar-date tags are stable MSYS2 releases; this excludes the rolling
# nightly-x86_64 release that GitHub otherwise reports as the latest release.
_STABLE_TAG = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})$")
_RELEASES_URL = "https://api.github.com/repos/msys2/msys2-installer/releases?per_page=100"


def check_update(context: dict[str, Any]) -> dict[str, str]:
    """Return metadata for the newest stable MSYS2 self-extracting archive.

    Parameters
    ----------
    context : dict[str, Any]
        Update context supplied by gupkg. The stable MSYS2 release feed is the
        authority for the selected version.

    Returns
    -------
    dict[str, str]
        Candidate metadata containing the version, archive URL, filename, and
        SHA-256 checksum required by the module payload.

    Raises
    ------
    RuntimeError
        The release feed cannot be read or contains no valid stable SFX asset.
    """
    del context

    # Request the official release feed so the selected asset is tied to one
    # date-tagged stable release rather than GitHub's rolling nightly release.
    request = urllib.request.Request(
        _RELEASES_URL, headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.load(response)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read the MSYS2 release feed: {exc}") from exc

    if not isinstance(releases, list):
        raise RuntimeError("MSYS2 release feed must contain a list of releases")

    # Examine stable tags newest-first and require the precisely named archive
    # with GitHub's published digest before exposing it to the update pipeline.
    stable_releases = sorted(
        (
            (match.group("date"), release)
            for release in releases
            if isinstance(release, dict)
            and isinstance((tag := release.get("tag_name")), str)
            and (match := _STABLE_TAG.fullmatch(tag)) is not None
        ),
        reverse=True,
    )
    for tag, release in stable_releases:
        version = tag.replace("-", "")
        filename = f"msys2-base-x86_64-{version}.sfx.exe"
        matches = [
            asset
            for asset in release.get("assets", [])
            if isinstance(asset, dict) and asset.get("name") == filename
        ]
        if len(matches) != 1:
            continue
        asset = matches[0]
        url = asset.get("browser_download_url")
        digest = asset.get("digest")
        if not isinstance(url, str) or not isinstance(digest, str):
            continue
        if not digest.startswith("sha256:") or len(digest) != 71:
            continue

        return {
            "candidateId": f"msys2:{tag}:{filename}",
            "version": version,
            "url": url,
            "fileName": filename,
            "sha256": digest.removeprefix("sha256:"),
        }

    raise RuntimeError(
        "MSYS2 release feed contains no stable msys2-base-x86_64-*.sfx.exe asset"
    )
