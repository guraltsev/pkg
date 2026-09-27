"""Discover the latest 64-bit 7-Zip executable from the official download page.

The checker reads 7-Zip's homepage, selects its current x64 Windows executable,
and supplies it to the package manager as an update candidate. The publisher
does not expose a checksum next to this link, so the manifest explicitly opts
out of checksum verification.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate describes a newer 64-bit Windows executable, if
one is available.

Implementation Approach
-----------------------
The checker derives the release version from the homepage's x64 release
announcement and pairs it with the matching versioned x64 executable anchor.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


PKG_MODULE_API = 1

# 7-Zip's homepage is the publisher's canonical current-release announcement.
_DOWNLOAD_PAGE = "https://www.7-zip.org/"
_X64_ANNOUNCEMENT = re.compile(
    r"Download\s+7-Zip\s+(?P<version>\d+(?:\.\d+)+)\s*"
    r"\([^)]*\)\s*for\s+Windows\s+x64",
    re.IGNORECASE,
)
_X64_EXECUTABLE = re.compile(r"^7z\d+-x64\.exe$", re.IGNORECASE)


class _DownloadPageParser(HTMLParser):
    """Collect page text and anchor targets from the 7-Zip download page."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.text: list[str] = []

    def handle_data(self, data: str) -> None:
        """Record visible page text used to identify the current release."""
        self.text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record links published by the official download page."""
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.hrefs.append(href)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the current x64 7-Zip executable when it is newer.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the currently installed package version.

    Returns
    -------
    dict[str, str] | None
        Candidate executable metadata, or ``None`` when the installed version
        is current or newer.

    Raises
    ------
    RuntimeError
        The official download page cannot be read or does not publish the
        expected x64 release announcement and executable link.
    """
    # Read the publisher's release announcement rather than constructing an
    # artifact URL from a separately maintained version source.
    try:
        with urllib.request.urlopen(_DOWNLOAD_PAGE, timeout=30) as response:
            page = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the 7-Zip download page: {exc}") from exc

    # Identify the announced x64 release before accepting a download link, so
    # installer links for 32-bit and ARM64 Windows builds are not selected.
    parser = _DownloadPageParser()
    parser.feed(page)
    announcement = _X64_ANNOUNCEMENT.search(" ".join(parser.text))
    if announcement is None:
        raise RuntimeError("7-Zip download page has no x64 release announcement")
    version = announcement.group("version")

    executable_url = next(
        (
            urllib.parse.urljoin(_DOWNLOAD_PAGE, href)
            for href in parser.hrefs
            if _X64_EXECUTABLE.fullmatch(
                urllib.parse.unquote(urllib.parse.urlparse(href).path).rsplit("/", 1)[-1]
            )
        ),
        None,
    )
    if executable_url is None:
        raise RuntimeError("7-Zip download page has no x64 executable link")

    current_version = context.get("current", {}).get("version")
    if (
        isinstance(current_version, str)
        and current_version != "bootstrap"
        and _compare_versions(version, current_version) <= 0
    ):
        return None

    filename = urllib.parse.unquote(urllib.parse.urlparse(executable_url).path).rsplit(
        "/", 1
    )[-1]
    return {
        "candidateId": f"7zip:{version}:{filename}",
        "version": version,
        "url": executable_url,
        "fileName": filename,
    }


def _compare_versions(left: str, right: str) -> int:
    """Compare dotted numeric release versions without manager internals."""
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)
