"""Discover the latest 64-bit portable SumatraPDF ZIP release.

The updater reads SumatraPDF's static download page, selects its matching
64-bit ZIP link, and exposes that archive to the package manager's ZIP payload
workflow. The publisher does not provide a checksum with this discovery page,
so the package manifest explicitly opts out of checksum verification.

Usage and API
-------------
The package manager calls ``check_update(context)`` during its module update
check. The returned candidate contains the discovered release version and ZIP
URL.

Implementation Approach
-----------------------
An HTML parser records the page's anchor targets. The checker accepts only the
versioned 64-bit ZIP href, excluding installer, ARM, and 32-bit payloads.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


PKG_MODULE_API = 1

_DOWNLOAD_PAGE = "https://www.sumatrapdfreader.org/download-free-pdf-viewer"
_ZIP_LINK = re.compile(
    r"^/dl/rel/(?P<version>[^/]+)/SumatraPDF-(?P=version)-64\.zip$"
)
# SumatraPDF rejects its archive URL when Python's default user agent is used.
_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class _DownloadLinkParser(HTMLParser):
    """Collect href values from the anchors in a download page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record each anchor's href attribute when it is present."""
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.links.append(href)


def check_update(context: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest portable 64-bit SumatraPDF ZIP when it is newer.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the currently installed package version.

    Returns
    -------
    dict[str, str] | None
        Candidate ZIP metadata, or ``None`` when the discovered version is not
        newer than the installed package.

    Raises
    ------
    RuntimeError
        The download page cannot be read or does not contain one matching ZIP
        link.
    """
    # Ask the publisher's static download page for the canonical release link
    # instead of constructing a version from an unrelated release feed.
    request = urllib.request.Request(
        _DOWNLOAD_PAGE, headers={"User-Agent": "gupkg-sumatrapdf-update-check/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the SumatraPDF download page: {exc}") from exc

    # Accept the exact versioned href published for the portable 64-bit ZIP.
    parser = _DownloadLinkParser()
    parser.feed(page)
    matches: list[tuple[str, str]] = []
    for href in parser.links:
        match = _ZIP_LINK.fullmatch(href)
        if match is None:
            continue
        version = match.group("version")
        matches.append((version, href))
    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) != 1:
        raise RuntimeError(
            "SumatraPDF download page must contain exactly one matching "
            "64-bit portable ZIP link"
        )

    version, href = unique_matches[0]
    current_version = context.get("current", {}).get("version")
    if (
        isinstance(current_version, str)
        and current_version != "bootstrap"
        and _compare_versions(version, current_version) <= 0
    ):
        return None
    filename = f"SumatraPDF-{version}-64.zip"
    return {
        "candidateId": f"sumatrapdf:{version}:{filename}",
        "version": version,
        "url": urllib.parse.urljoin(_DOWNLOAD_PAGE, href),
        "fileName": filename,
        "headers": {"User-Agent": _DOWNLOAD_USER_AGENT},
    }


def _compare_versions(left: str, right: str) -> int:
    """Compare dotted numeric release versions without importing manager internals."""
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)
