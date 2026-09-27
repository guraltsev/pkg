"""Resolve and run the archive extractor shipped with the runtime.

Package-local hooks use :func:`find_7z` when they need to unpack an archive
format supported by 7-Zip. The packaged executable is preferred so extraction
does not depend on a host installation, while the familiar system commands
remain available for source checkouts or installations without package data.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def find_7z() -> Path:
    """Return the first available bundled or system 7-Zip executable.

    Returns
    -------
    pathlib.Path
        The bundled ``7z.exe`` when present, otherwise the first matching
        ``7z``, ``7za``, or ``7zr`` executable on ``PATH``.

    Raises
    ------
    FileNotFoundError
        If neither the bundled executable nor a supported system command is
        available.
    """
    # Keep the package-owned extractor independent of the current working
    # directory so package-local hooks work from any package root.
    bundled = Path(__file__).resolve().parent / "7zip" / "7z.exe"
    if bundled.is_file():
        return bundled

    # Preserve the documented fallback order when the package data is absent.
    for command_name in ("7z", "7za", "7zr"):
        command = shutil.which(command_name)
        if command:
            return Path(command)

    raise FileNotFoundError(
        "No 7-Zip extractor is available; install bundled 7z or add 7z, 7za, "
        "or 7zr to PATH"
    )
