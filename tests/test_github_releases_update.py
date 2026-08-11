"""Cover update discovery through GitHub's latest-release REST endpoint.

The HTTP response boundary is mocked while repository URL conversion, release
parsing, exact asset selection, and candidate construction are real. Artifact
download and extraction are covered by the broader CLI behavior suite and are
out of scope here.
"""

from __future__ import annotations

import io
import json
from unittest import mock

import pytest

from gupkg.configuration import normalize_runtime_config
from gupkg.core import ConfigValidationError, PackageIdentity
from gupkg import github_releases


def release_response(*, tag: str = "v1.13.0") -> io.BytesIO:
    """Return representative GitHub latest-release metadata as a byte stream."""
    release = {
        "id": 12345,
        "tag_name": tag,
        "html_url": "https://github.com/garethgeorge/backrest/releases/tag/v1.13.0",
        "published_at": "2026-05-04T05:30:41Z",
        "assets": [
            {
                "name": "backrest_Linux_x86_64.tar.gz",
                "state": "uploaded",
                "browser_download_url": "https://example.invalid/linux.tar.gz",
            },
            {
                "name": "backrest_Windows_x86_64.zip",
                "state": "uploaded",
                "browser_download_url": (
                    "https://github.com/garethgeorge/backrest/releases/"
                    "download/v1.13.0/backrest_Windows_x86_64.zip"
                ),
                "digest": "sha256:" + "ab" * 32,
            },
        ],
    }
    return io.BytesIO(json.dumps(release).encode())


def test_latest_release_selects_the_mandatory_named_asset() -> None:
    """A repository origin and exact asset name produce a verified candidate."""
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "backrest_Windows_x86_64.zip",
        "current": {"version": "1.12.1"},
    }

    with mock.patch.object(
        github_releases.urllib.request,
        "urlopen",
        return_value=release_response(),
    ) as urlopen:
        candidate = github_releases.check_update(context)

    assert candidate == {
        "candidateId": "github-release:garethgeorge/backrest:12345",
        "version": "1.13.0",
        "url": (
            "https://github.com/garethgeorge/backrest/releases/"
            "download/v1.13.0/backrest_Windows_x86_64.zip"
        ),
        "fileName": "backrest_Windows_x86_64.zip",
        "sha256": "ab" * 32,
        "publishedAt": "2026-05-04T05:30:41Z",
        "notesUrl": (
            "https://github.com/garethgeorge/backrest/releases/tag/v1.13.0"
        ),
    }
    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "https://api.github.com/repos/garethgeorge/backrest/releases/latest"
    )


def test_current_release_returns_no_candidate() -> None:
    """A current installed version does not produce an update candidate."""
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "backrest_Windows_x86_64.zip",
        "current": {"version": "1.13.0"},
    }

    with mock.patch.object(
        github_releases.urllib.request,
        "urlopen",
        return_value=release_response(),
    ):
        candidate = github_releases.check_update(context)

    assert candidate is None


def test_current_release_repairs_a_missing_application_payload() -> None:
    """A current version remains downloadable when its App payload is missing."""
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "backrest_Windows_x86_64.zip",
        "current": {"version": "1.13.0", "appReady": False},
    }

    with mock.patch.object(
        github_releases.urllib.request,
        "urlopen",
        return_value=release_response(),
    ):
        candidate = github_releases.check_update(context)

    assert candidate is not None
    assert candidate["version"] == "1.13.0"


def test_latest_release_expands_version_in_asset_name() -> None:
    """A versioned asset template selects the asset from the latest release."""
    response = release_response()
    release = json.loads(response.getvalue())
    release["assets"][1]["name"] = "backrest-1.13.0-windows-x86_64.zip"
    release["assets"][1]["browser_download_url"] = (
        "https://github.com/garethgeorge/backrest/releases/"
        "download/v1.13.0/backrest-1.13.0-windows-x86_64.zip"
    )
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "backrest-${version}-windows-x86_64.zip",
        "current": {"version": "1.12.1"},
    }

    with mock.patch.object(
        github_releases.urllib.request,
        "urlopen",
        return_value=io.BytesIO(json.dumps(release).encode()),
    ):
        candidate = github_releases.check_update(context)

    assert candidate is not None
    assert candidate["fileName"] == "backrest-1.13.0-windows-x86_64.zip"


def test_latest_release_strips_a_configured_tag_prefix() -> None:
    """A publisher tag namespace does not become part of the package version."""
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "backrest_Windows_x86_64.zip",
        "tagPrefix": "release/",
        "current": {"version": "1.12.1"},
    }

    with mock.patch.object(
        github_releases.urllib.request,
        "urlopen",
        return_value=release_response(tag="release/v1.13.0"),
    ):
        candidate = github_releases.check_update(context)

    assert candidate is not None
    assert candidate["version"] == "1.13.0"


def test_missing_named_asset_fails_clearly() -> None:
    """A release without the configured asset reports the exact missing name."""
    context = {
        "url": "https://github.com/garethgeorge/backrest",
        "assetName": "portable.zip",
        "current": {"version": "1.12.1"},
    }

    with (
        mock.patch.object(
            github_releases.urllib.request,
            "urlopen",
            return_value=release_response(),
        ),
        pytest.raises(RuntimeError, match=r"portable\.zip"),
    ):
        github_releases.check_update(context)


def test_github_check_uses_origin_url_and_requires_asset_name(tmp_path) -> None:
    """GitHub mode takes its repository from origin and rejects no asset name."""
    package_root = tmp_path / "Tool"
    version_path = package_root / "vbootstrap.l1"
    identity = PackageIdentity.from_version_path(
        package_root,
        version_path,
        is_current=False,
    )
    config = {
        "origin": {"url": "https://github.com/owner/tool"},
        "update": {
            "check": {
                "mode": "github",
                "assetName": "tool_Windows_x86_64.zip",
                "tagPrefix": "release/",
            },
            "payload": {"mode": "zip"},
        },
    }

    normalized = normalize_runtime_config(config, identity)

    assert normalized["update"]["check"] == {
        "mode": "github",
        "url": "https://github.com/owner/tool",
        "assetName": "tool_Windows_x86_64.zip",
        "tagPrefix": "release/",
    }
    del config["update"]["check"]["assetName"]
    with pytest.raises(ConfigValidationError, match="assetName"):
        normalize_runtime_config(config, identity)
