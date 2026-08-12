"""Populate qBittorrent's application tree by unpacking its Windows installer."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def populate_app(context: dict[str, Any]) -> None:
    """Extract the configured qBittorrent release into the managed App directory.

    Parameters
    ----------
    context : dict[str, Any]
        Origin context containing the package identity and managed application path.
    """
    version = context["identity"]["version"]
    stage_app = Path(context["PkgVars"]["App"])
    filename = f"qbittorrent_{version}_x64_setup.exe"
    download_url = (
        "https://sourceforge.net/projects/qbittorrent/files/"
        f"qbittorrent-win32/qbittorrent-{version}/{filename}/download"
    )

    # Keep the installer outside the package tree; only its installed files
    # belong in App and remain after origin population completes.
    with tempfile.TemporaryDirectory(prefix="gupkg-qbittorrent-") as temp_root:
        installer = Path(temp_root) / filename
        with urllib.request.urlopen(download_url, timeout=60) as response:
            with installer.open("wb") as handle:
                shutil.copyfileobj(response, handle)

        # Extract the installer archive into the manager-owned App directory
        # without running NSIS installation actions against the host system.
        stage_app.mkdir(parents=True, exist_ok=True)
        command = subprocess.list2cmdline(
            ["7z", "x", "-y", f"-o{stage_app}", str(installer)]
        )
        subprocess.run(command, check=True, shell=True)
