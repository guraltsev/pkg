"""Cover the user-visible behaviour of ``gupkg-bootstrap.cmd`` / ``gupkg-bootstrap.ps1``.

The wrapper, the real PowerShell script, a real ZIP of this repository's
``src`` folder, and the installed launcher are used. ``-SkipInstall`` is passed
everywhere, so no test changes the registry, PATH, or Start Menu, and no network
is used. The ``gupkg install`` / ``manager init`` steps that follow placement
are covered by the install and manager tests, not here.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "gupkg-bootstrap.cmd"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the bootstrap script targets Windows PowerShell")


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A GitHub-style repository archive (``pkg-stable/src/...``) built from this checkout."""
    path = tmp_path_factory.mktemp("bootstrap") / "repo.zip"
    src = REPO / "src"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        for file in src.rglob("*"):
            relative = file.relative_to(src)
            if file.is_file() and "__pycache__" not in relative.parts and relative.parts[:1] != ("python",):
                zipped.write(file, Path("pkg-stable") / "src" / relative)
        zipped.writestr("pkg-stable/pkgs/other/readme.txt", "not part of gupkg")
    return path


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the bootstrap wrapper, always placing files only, under a temporary root."""
    return subprocess.run(
        [str(BOOTSTRAP), "-Root", str(tmp_path / "opt"), "-SkipInstall", *arguments],
        capture_output=True,
        text=True,
        timeout=100,
    )


def test_verified_archive_is_placed_as_a_working_versioned_folder_and_rerun_reuses_it(
    tmp_path: Path, archive: Path
) -> None:
    """A verified archive lands in <root>\\gupkg\\v<version>, runs, and a rerun is a no-op."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest().upper()

    first = _run(tmp_path, "-Source", str(archive), "-Sha256", digest)
    placed = next((tmp_path / "opt" / "gupkg").glob("v*"))
    second = _run(tmp_path, "-Source", str(archive), "-Sha256", digest)
    version = subprocess.run([str(placed / "gupkg.cmd"), "--version"], capture_output=True, text=True)

    assert first.returncode == 0 and "SHA-256 verified" in first.stdout
    assert "reusing it" in second.stdout and second.returncode == 0
    assert version.stdout.strip() == f"gupkg {placed.name[1:]}"
    assert not (placed / "pkgs").exists()


def test_checksum_mismatch_stops_before_anything_is_placed(tmp_path: Path, archive: Path) -> None:
    """A wrong -Sha256 fails with a clear message and creates no install folder."""
    result = _run(tmp_path, "-Source", str(archive), "-Sha256", "0" * 64)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_system_scope_requires_an_elevated_shell(tmp_path: Path, archive: Path) -> None:
    """System scope from a non-elevated shell explains how to proceed instead of failing midway."""
    principal = subprocess.run(
        ["net", "session"], capture_output=True, text=True
    )
    if principal.returncode == 0:
        pytest.skip("the test process is elevated")

    result = _run(tmp_path, "-Scope", "system", "-Source", str(archive))

    assert result.returncode == 1
    assert "elevated PowerShell" in result.stderr


def test_missing_source_is_reported_plainly(tmp_path: Path) -> None:
    """A source that is neither a URL, a ZIP, nor a folder is rejected with a clear error."""
    result = _run(tmp_path, "-Source", str(tmp_path / "nope.zip"))

    assert result.returncode == 1
    assert "Source not found" in result.stderr
