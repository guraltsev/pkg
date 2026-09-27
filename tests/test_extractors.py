"""Cover bundled-first 7-Zip resolution for package-local archive hooks.

The bundled executable location is real. System command discovery is mocked at
the PATH boundary so the tests do not depend on a host 7-Zip installation or
execute an archive extractor.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from gupkg import extractors


def test_find_7z_prefers_the_executable_below_the_loaded_gupkg() -> None:
    """The packaged 7z executable wins even when a system command exists."""
    with mock.patch.object(extractors.shutil, "which", return_value=r"C:\Tools\7z.exe"):
        executable = extractors.find_7z()

    assert executable.name == "7z.exe"
    assert executable.parent.name == "7zip"
    assert executable.is_file()


@pytest.mark.parametrize(
    "system_name",
    ("7z", "7za", "7zr"),
    ids=("system-7z", "system-7za", "system-7zr"),
)
def test_find_7z_uses_each_supported_system_fallback(
    system_name: str,
) -> None:
    """Each supported system command is accepted when the bundle is absent."""
    system_path = Path(r"C:\Tools") / f"{system_name}.exe"

    def which(command_name: str) -> str | None:
        return str(system_path) if command_name == system_name else None

    with mock.patch.object(extractors.Path, "is_file", return_value=False):
        with mock.patch.object(extractors.shutil, "which", side_effect=which):
            executable = extractors.find_7z()

    assert executable == system_path
