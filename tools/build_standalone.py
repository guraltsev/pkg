"""Assemble a relocatable standalone source package for release automation.

The script deliberately does not download Python or execute package-local
hooks. CI supplies a verified embedded-Python archive and its SHA-256 digest,
then this script copies the application and release metadata into a versioned
payload for the native bootstrap artifact.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path


def _source_version(source: Path) -> str:
    """Read the release value from the runtime's single version module."""
    version_source = source / "_version.py"
    tree = ast.parse(version_source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise ValueError(f"No string __version__ found in {version_source}")


def main() -> int:
    """Build a standalone ZIP from a checked-out source tree and runtime ZIP."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--source", type=Path, default=Path("src/gupkg"))
    parser.add_argument("--manifest", type=Path, default=Path("src/gupkg/pkg.toml"))
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # The runtime module owns the release number; an optional command-line
    # value is only an assertion for release scripts, never an override.
    release_version = _source_version(args.source)
    if args.version is not None and args.version != release_version:
        raise SystemExit(
            f"--version {args.version!r} does not match the runtime version {release_version!r}"
        )
    manifest_data = tomllib.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest_data.get("version") != release_version:
        raise SystemExit(
            f"standalone manifest version {manifest_data.get('version')!r} "
            f"does not match runtime version {release_version!r}"
        )
    digest = hashlib.sha256(args.runtime.read_bytes()).hexdigest()
    if digest.casefold() != args.runtime_sha256.casefold():
        raise SystemExit("embedded runtime digest does not match --runtime-sha256")
    with tempfile.TemporaryDirectory(prefix="gupkg-standalone-") as temporary:
        root = Path(temporary) / f"v{release_version}"
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
        # Keep manager discovery deterministic in a standalone payload while
        # leaving user roaming configuration as the normal override.
        (payload / "gupkg-config.toml").write_text(
            'mode = "manager"\n'
            'schema_version = 2\n\n'
            '[packages]\n'
            'system = "../system"\n'
            'user = "../user"\n\n'
            '[bin]\n'
            'system = "../bin-system"\n'
            'user = "../bin-user"\n\n'
            '[registry]\n'
            'cache = "../registry"\n'
            'channel = "stable"\n\n'
            '[shims]\n'
            'linkage = "dynamic"\n',
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
