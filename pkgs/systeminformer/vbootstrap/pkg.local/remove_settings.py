"""Remove machine-specific System Informer settings from staged release payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def install_step(context: dict[str, Any]) -> None:
    """Remove extracted settings files from every supported System Informer build.

    Parameters
    ----------
    context : dict[str, Any]
        Staged update context supplied by gupkg.
    """
    app_path = Path(context["paths"]["stageApp"])

    # Each release carries separate architecture directories.  Remove the
    # generated settings file when present without requiring every build to be
    # included in a particular upstream archive.
    for architecture in ("arm64", "amd64", "i386"):
        (app_path / architecture / "SystemInformer.exe.settings.xml").unlink(
            missing_ok=True
        )
