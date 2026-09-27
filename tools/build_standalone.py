"""Assemble a relocatable standalone source package for release automation.

The script deliberately does not download Python or execute package-local
hooks. CI supplies a verified embedded-Python archive and its SHA-256 digest,
then this script copies the application and release metadata into a versioned
payload for the native bootstrap artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path


def main() -> int:
    """Build a standalone ZIP from a checked-out source tree and runtime ZIP."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--source", type=Path, default=Path("src/gupkg"))
    parser.add_argument("--manifest", type=Path, default=Path("pkgs/gupkg/vbootstrap/pkg.toml"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest = hashlib.sha256(args.runtime.read_bytes()).hexdigest()
    if digest.casefold() != args.runtime_sha256.casefold():
        raise SystemExit("embedded runtime digest does not match --runtime-sha256")
    with tempfile.TemporaryDirectory(prefix="gupkg-standalone-") as temporary:
        root = Path(temporary) / f"v{args.version}"
        payload = root / "gupkg"
        payload.mkdir(parents=True)
        shutil.copytree(args.source, payload, dirs_exist_ok=True, ignore=shutil.ignore_patterns("python"))
        # Materialize the audited native launchers and their relocatable
        # relative configurations as package-owned support files.
        shim = args.source / "shim" / "shim-console.exe"
        if shim.is_file():
            shutil.copy2(shim, payload / "gupkg.exe")
            shutil.copy2(shim, payload / "gupkg-tui.exe")
        (payload / "gupkg.config.toml").write_text(
            'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n[[argument]]\nvalue = "gupkg.cmd"\n',
            encoding="utf-8",
        )
        (payload / "gupkg-tui.config.toml").write_text(
            'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n[[argument]]\nvalue = "gupkg-tui.cmd"\n',
            encoding="utf-8",
        )
        for wrapper_name in ("gupkg.cmd", "gupkg-tui.cmd"):
            wrapper = args.source.parent / wrapper_name
            if wrapper.is_file():
                shutil.copy2(wrapper, root / wrapper_name)
        shutil.copy2(args.manifest, root / "pkg.toml")
        with zipfile.ZipFile(args.runtime) as runtime_zip:
            runtime_zip.extractall(payload / "python")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
