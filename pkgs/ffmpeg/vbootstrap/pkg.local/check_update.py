"""Discover the current Gyan.dev FFmpeg release essentials ZIP for Windows.

The checker reads Gyan.dev's version and checksum endpoints for the stable
release essentials archive, then returns the archive metadata needed for a
verified ZIP update. It does not select git-master or full build variants.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate describes the current release essentials ZIP,
unless the installed package has an equal or newer healthy version.

Implementation Approach
-----------------------
The checker consumes the publisher's compact text endpoints rather than
parsing the builds page, validates the numeric version and SHA-256 digest, and
constructs the documented stable ZIP download URL.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

# Gyan.dev publishes stable machine-readable endpoints for this exact archive.
_ARCHIVE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
_CHECKSUM_URL = f"{_ARCHIVE_URL}.sha256"
_VERSION = re.compile(r"\d+(?:\.\d+)+")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the current Gyan.dev release essentials ZIP when required.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the currently installed package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate ZIP metadata, or ``None`` when the installed release is
        healthy and current or newer.

    Raises
    ------
    RuntimeError
        The publisher endpoints cannot be read or publish an invalid version
        or SHA-256 digest.
    """
    # Read the publisher's compact version endpoint so release discovery does
    # not depend on the presentation structure of the builds page.
    version = _read_text(_VERSION_URL, "FFmpeg release version")
    if _VERSION.fullmatch(version) is None:
        raise RuntimeError(f"Gyan.dev published an invalid FFmpeg release version: {version!r}")

    # A healthy equal-or-newer package needs neither an archive download nor a
    # checksum lookup. An incomplete payload returns the current candidate for repair.
    current = context.get("current", {})
    current_version = current.get("version") if isinstance(current, dict) else None
    if (
        isinstance(current_version, str)
        and _VERSION.fullmatch(current_version) is not None
        and _compare_versions(version, current_version) <= 0
        and current.get("appReady", True)
    ):
        return None

    # Fetch the checksum that corresponds to the stable archive URL that will
    # be staged, preserving verification across the publisher's redirects.
    digest = _read_text(_CHECKSUM_URL, "FFmpeg release essentials ZIP checksum")
    if _SHA256.fullmatch(digest) is None:
        raise RuntimeError("Gyan.dev FFmpeg release essentials checksum is not a SHA-256 digest")

    filename = "ffmpeg-release-essentials.zip"
    return {
        "candidateId": f"ffmpeg:{version}:{filename}",
        "version": version,
        "url": _ARCHIVE_URL,
        "fileName": filename,
        "sha256": digest.lower(),
    }


def _read_text(url: str, description: str) -> str:
    """Read one UTF-8 publisher endpoint and normalize surrounding whitespace."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read {description}: {exc}") from exc


def _compare_versions(left: str, right: str) -> int:
    """Compare numeric dotted FFmpeg release versions with zero-filled tails."""
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)
