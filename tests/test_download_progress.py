"""Cover download size and progress feedback visible to CLI users.

The ZIP extraction and CLI rendering boundaries are real. HTTP responses and
the package workflow are mocked only where they isolate response metadata or
the output-format contract; archive contents and filesystem writes remain real.
"""

from __future__ import annotations

import io
from pathlib import Path
import zipfile

import pytest

from gupkg import cli
from gupkg import gupkg as package_workflows
from gupkg import origin
from gupkg.core import ActionResult, PackageIdentity, log_info


class _Response(io.BytesIO):
    """Provide a bytes response with the HTTP headers used by the downloader."""

    def __init__(self, content: bytes, *, content_length: int | None) -> None:
        super().__init__(content)
        self.headers = (
            {"Content-Length": str(content_length)}
            if content_length is not None
            else {}
        )


def _archive() -> bytes:
    """Return a small valid application archive for origin population."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("tool.exe", "payload")
    return archive.getvalue()


def test_zip_origin_reports_total_size_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A known-length origin download shows its total and reaches 100 percent."""
    version_path = tmp_path / "Example" / "v1.0.0"
    version_path.mkdir(parents=True)
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )
    archive = _archive()
    response = _Response(archive, content_length=len(archive))
    monkeypatch.setattr(origin.urllib.request, "urlopen", lambda _url, **_kwargs: response)

    origin.populate_app_from_zip_origin(
        identity,
        {"url": "https://example.invalid/app.zip"},
        no_checksum=False,
    )

    output = capsys.readouterr().out
    assert f"Downloading origin: total size {len(archive)} B" in output
    assert "Downloading origin:" in output and "(100%)" in output
    assert (version_path / "App" / "tool.exe").is_file()


def test_human_cli_streams_progress_while_toml_keeps_stdout_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human output exposes progress while TOML output contains only its document."""
    version_path = tmp_path / "Example" / "v1.0.0"
    (version_path / "App").mkdir(parents=True)
    (version_path / "pkg.toml").write_text(
        'name = "Example"\nversion = "1.0.0"\nlocalVersion = 0\n',
        encoding="utf-8",
    )

    def fake_download(*_args, **_kwargs) -> ActionResult:
        log_info("Downloading update: total size 1.0 KiB")
        log_info("Downloading update: 1.0 KiB / 1.0 KiB (100%)")
        return ActionResult(True, changed=True, status="downloaded")

    monkeypatch.setattr(package_workflows, "download_package_update", fake_download)

    assert cli.main(["update", "--download-only", str(version_path)]) == 0
    human_output = capsys.readouterr().out
    assert "Downloading update: total size 1.0 KiB" in human_output
    assert "Downloading update:" in human_output and "(100%)" in human_output

    assert (
        cli.main(
            ["--format", "toml", "update", "--download-only", str(version_path)]
        )
        == 0
    )
    toml_output = capsys.readouterr().out
    assert "Downloading update" not in toml_output
    assert "output_schema = 1" in toml_output
