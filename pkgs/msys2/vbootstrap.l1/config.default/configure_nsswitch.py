"""Configure MSYS2 name-service settings for Windows home directories.

Run this script through ``configure_nsswitch.cmd`` after setting
``MSYS2_INSTALL_PATH`` to the MSYS2 installation directory. It updates the
installation's ``etc/nsswitch.conf`` in place and fails clearly when the
expected configuration or ``db_home`` setting is absent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def main() -> int:
    """Set the MSYS2 ``db_home`` setting to ``windows``.

    Returns
    -------
    int
        Zero after updating the setting or confirming it already has the
        requested value.

    Raises
    ------
    RuntimeError
        If the MSYS2 installation path, configuration file, or setting is
        unavailable.
    """
    install_path = os.environ.get("MSYS2_INSTALL_PATH")
    if not install_path:
        raise RuntimeError("MSYS2_INSTALL_PATH is not set.")

    # The bootstrap package promotes MSYS2's ``msys64`` contents directly into
    # this root, so its configuration lives at this stable relative path.
    nsswitch_path = Path(install_path) / "etc" / "nsswitch.conf"
    if not nsswitch_path.is_file():
        raise RuntimeError(f"MSYS2 nsswitch configuration was not found: {nsswitch_path}")

    # Replace only the setting value while preserving its indentation, line
    # endings, and the rest of the configuration as supplied by MSYS2.
    contents = nsswitch_path.read_bytes().decode("utf-8")
    updated_contents, replacements = re.subn(
        r"^([ \t]*db_home[ \t]*:[ \t]*)[^\r\n]*(\r?)$",
        r"\1windows\2",
        contents,
        count=1,
        flags=re.MULTILINE,
    )
    if not replacements:
        raise RuntimeError(f"The db_home setting was not found: {nsswitch_path}")

    # Avoid touching the file when MSYS2 already has the desired setting.
    if updated_contents != contents:
        nsswitch_path.write_bytes(updated_contents.encode("utf-8"))
        print(f"Updated db_home to windows in {nsswitch_path}")
    else:
        print(f"db_home is already set to windows in {nsswitch_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
