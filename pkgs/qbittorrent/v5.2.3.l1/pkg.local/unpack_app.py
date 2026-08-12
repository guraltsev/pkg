"""Extract the qBittorrent setup executable into the staged application tree.

The qBittorrent Windows release is an NSIS installer rather than a ZIP or
portable executable. This package-local updater uses 7-Zip to unpack its
embedded files into the package manager's staged ``App/`` directory.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged application tree used for atomic
package activation.

Implementation Approach
-----------------------
The installer archive is extracted directly into the staged application tree,
which avoids executing installer actions or modifying system installation
state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the downloaded qBittorrent executable into staged ``App/``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Extract the embedded runtime files into the isolated staging tree so
    # update activation cannot trigger installer-managed system changes.
    stage_app.mkdir(parents=True)
    command = subprocess.list2cmdline(
        ["7z", "x", "-y", f"-o{stage_app}", str(artifact)]
    )
    subprocess.run(command, check=True, shell=True)
