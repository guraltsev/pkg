"""Extract an MSYS2 self-extracting archive into a staged application tree.

The MSYS2 ``.sfx.exe`` asset is an archive, not a GUI installer. Running it
with extraction-only arguments populates the package manager's isolated staging
directory without creating installer-managed Windows integration.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a verified
MSYS2 archive. The function leaves the extracted files under ``stageApp``.

Implementation Approach
-----------------------
The archive executable receives the 7-Zip-compatible non-interactive extract
arguments and runs directly against the staging directory before activation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the verified MSYS2 archive into the staged ``App`` directory.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded archive and staging paths.

    Raises
    ------
    subprocess.CalledProcessError
        The self-extracting archive exits unsuccessfully.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Create the destination first so the archive can write only inside the
    # package manager's disposable staging tree.
    stage_app.mkdir(parents=True)
    subprocess.run(
        [str(artifact), "-y", f"-o{stage_app}"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
