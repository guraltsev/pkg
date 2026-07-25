"""Discover downloadable updates from a GitHub repository's latest release.

The update checker accepts a normal ``https://github.com/owner/repository``
origin URL, queries GitHub's latest-release REST endpoint, and returns the
configured release asset as an update candidate. An ``assetName`` may include
``${version}``, which expands to the latest release version.

Usage and API
-------------
Call ``check_update(context)`` with the repository URL, mandatory
``assetName``, and current package identity. The package update coordinator
uses this function for ``[update.check].mode = "github"``.

Implementation Approach
-----------------------
The origin URL is validated and converted to the corresponding GitHub API
endpoint. Release metadata and asset fields are validated before the exactly
named asset, optionally derived from the release version, is exposed to the
package manager's existing download, checksum, and extraction workflow.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any


PKG_MODULE_API = 1


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the latest GitHub release asset when an update is available.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the origin ``url``, mandatory ``assetName``,
        and the current package version. ``assetName`` may contain one or more
        ``${version}`` placeholders for the latest release version.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata for the selected release asset, or ``None`` when
        the installed version matches the latest release tag.

    Raises
    ------
    RuntimeError
        The repository URL, API response, release tag, or asset selection is
        invalid or the GitHub request fails.

    Examples
    --------
    Basic usage:

    >>> context = {
    ...     "url": "https://github.com/owner/tool",
    ...     "assetName": "tool_Windows_x86_64.zip",
    ...     "current": {"version": "1.0.0"},
    ... }
    >>> context["url"]
    'https://github.com/owner/tool'
    """
    # Accept repository page URLs only, keeping API construction and host
    # selection under package-manager control.
    repository_url = context.get("url")
    if not isinstance(repository_url, str):
        raise RuntimeError("GitHub update URL must be a repository URL")
    parsed = urllib.parse.urlparse(repository_url)
    path_parts = [part for part in parsed.path.rstrip("/").split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 2
    ):
        raise RuntimeError(
            "GitHub update URL must have the form "
            "https://github.com/owner/repository"
        )
    owner, repository = path_parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise RuntimeError(
            "GitHub update URL must have the form "
            "https://github.com/owner/repository"
        )

    # Request the stable release selected by GitHub. This endpoint excludes
    # drafts and prereleases according to GitHub's latest-release semantics.
    api_url = (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repository, safe='')}/releases/latest"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pkg-github-release-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the latest GitHub release: {exc}") from exc

    # GitHub tags commonly carry a cosmetic "v" prefix while pkg stores the
    # upstream portion without the version-directory marker.
    if not isinstance(release, dict):
        raise RuntimeError("GitHub latest release response must be an object")
    tag = release.get("tag_name")
    release_id = release.get("id")
    if not isinstance(tag, str) or not tag.strip() or not isinstance(release_id, int):
        raise RuntimeError("GitHub latest release has no valid tag or release ID")
    version = tag[1:] if re.fullmatch(r"v\d.*", tag, re.IGNORECASE) else tag
    if context.get("current", {}).get("version") in {tag, version}:
        return None

    # Substitute the discovered version before selecting one exact uploaded
    # asset. This retains platform safety while allowing versioned filenames.
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("GitHub latest release has no valid asset list")
    asset_name = context.get("assetName")
    if not isinstance(asset_name, str) or not asset_name:
        raise RuntimeError("GitHub assetName must be a non-empty string")
    expected_asset_name = asset_name.replace("${version}", version)
    candidates = []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("state") != "uploaded":
            continue
        name = asset.get("name")
        if name == expected_asset_name:
            candidates.append(asset)
    if len(candidates) != 1:
        raise RuntimeError(
            "GitHub latest release must contain exactly one uploaded asset named "
            f"{expected_asset_name!r}; found {len(candidates)}"
        )

    # Return only fields understood by the update coordinator. GitHub's asset
    # digest is optional, so checksum policy remains with [update.payload].
    asset = candidates[0]
    name = asset.get("name")
    download_url = asset.get("browser_download_url")
    if not isinstance(download_url, str) or not isinstance(name, str):
        raise RuntimeError("Selected GitHub release asset has no download URL")
    result = {
        "candidateId": f"github-release:{owner}/{repository}:{release_id}",
        "version": version,
        "url": download_url,
        "fileName": name,
    }
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        result["sha256"] = digest.removeprefix("sha256:")
    published_at = release.get("published_at")
    if isinstance(published_at, str):
        result["publishedAt"] = published_at
    notes_url = release.get("html_url")
    if isinstance(notes_url, str):
        result["notesUrl"] = notes_url
    return result
