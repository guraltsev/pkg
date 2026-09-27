"""Extract the 7-Zip Windows executable into the staged application tree.

The official Windows executable is a self-extracting archive. This module
uses the package manager's bundled 7-Zip extractor to populate ``App`` without
executing an installer or modifying system installation state.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The extractor receives the downloaded executable and the manager-owned staging
directory, keeping all archive output isolated until activation succeeds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from gupkg.extractors import find_7z


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the downloaded 7-Zip executable into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Keep extracted files within the staged tree so the self-extracting
    # archive cannot perform installer actions against the live system.
    stage_app.mkdir(parents=True)
    command = [str(find_7z()), "x", "-y", f"-o{stage_app}", str(artifact)]
    subprocess.run(command, check=True)
