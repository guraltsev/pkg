"""Install the qBittorrent setup executable into the staged application tree.

The qBittorrent Windows release is an NSIS installer rather than a ZIP or
portable executable. This package-local updater invokes its documented silent
installation mode with the package manager's staged ``App/`` directory.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged application tree used for atomic
package activation.

Implementation Approach
-----------------------
The installer runs once with NSIS's silent and destination arguments. The
destination argument is last, as required by NSIS, so no files are written
outside the manager-owned staging directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Install the downloaded qBittorrent executable into staged ``App/``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Direct the silent NSIS installation into the new version's isolated App
    # directory so the package manager can activate it atomically afterward.
    stage_app.mkdir(parents=True)
    subprocess.run([str(artifact), "/S", f"/D={stage_app}"], check=True)
