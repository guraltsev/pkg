"""Discover the newest portable 64-bit VLC ZIP release.

The updater reads VideoLAN's release-directory index, chooses the highest
numeric version, and obtains that version's official 64-bit Windows ZIP and
SHA-256 sidecar. It supplies the package manager with the archive metadata
needed to verify and extract the release.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate describes a newer VLC archive, if one exists.

Implementation Approach
-----------------------
The checker accepts only numeric release-directory names, compares their
dotted version components, and constructs the publisher's stable archive
paths. The SHA-256 value is read from the matching sidecar before staging.
"""

from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser
from typing import Any


PKG_MODULE_API = 1

# VideoLAN's directory index is the authoritative list of stable VLC releases.
_RELEASE_INDEX = "https://download.videolan.org/pub/videolan/vlc/"
_VERSION_DIRECTORY = re.compile(r"^(?P<version>\d+(?:\.\d+)+)/$")
_SHA256 = re.compile(r"\b(?P<digest>[0-9a-fA-F]{64})\b")


class _DirectoryLinkParser(HTMLParser):
    """Collect href values from the VideoLAN directory listing."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record each anchor href that the index publishes."""
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.links.append(href)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest 64-bit VLC ZIP release when it is newer.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the currently installed package version.

    Returns
    -------
    dict[str, str] | None
        Candidate ZIP metadata, or ``None`` when the installed version is
        current or newer.

    Raises
    ------
    RuntimeError
        The VideoLAN index or the selected archive checksum cannot be read,
        or the index does not publish a numeric release directory.
    """
    # Read the public index because it is VideoLAN's authoritative stable
    # release list and does not depend on a separately maintained API.
    try:
        with urllib.request.urlopen(_RELEASE_INDEX, timeout=30) as response:
            index = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the VideoLAN VLC release index: {exc}") from exc

    # Keep only numeric version folders, excluding directory aliases such as
    # ``last/`` and distribution-specific folders.
    parser = _DirectoryLinkParser()
    parser.feed(index)
    versions = {
        match.group("version")
        for href in parser.links
        if (match := _VERSION_DIRECTORY.fullmatch(href)) is not None
    }
    if not versions:
        raise RuntimeError("VideoLAN VLC release index contains no numeric versions")
    version = max(versions, key=_version_key)

    current_version = context.get("current", {}).get("version")
    if (
        isinstance(current_version, str)
        and current_version != "bootstrap"
        and _compare_versions(version, current_version) <= 0
    ):
        return None

    # Fetch the sidecar matching the exact archive that will be downloaded so
    # the package manager can verify the publisher-provided SHA-256 digest.
    filename = f"vlc-{version}-win64.zip"
    archive_url = f"{_RELEASE_INDEX}{version}/win64/{filename}"
    try:
        with urllib.request.urlopen(f"{archive_url}.sha256", timeout=30) as response:
            checksum_text = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the VLC archive checksum: {exc}") from exc
    match = _SHA256.search(checksum_text)
    if match is None:
        raise RuntimeError("VLC archive checksum does not contain a SHA-256 digest")

    return {
        "candidateId": f"vlc:{version}:{filename}",
        "version": version,
        "url": archive_url,
        "fileName": filename,
        "sha256": match.group("digest").lower(),
    }


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering VLC versions."""
    return tuple(int(part) for part in version.split("."))


def _compare_versions(left: str, right: str) -> int:
    """Compare dotted numeric versions while treating missing parts as zero."""
    left_parts = _version_key(left)
    right_parts = _version_key(right)
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)
