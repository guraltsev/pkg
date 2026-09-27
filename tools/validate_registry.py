"""Validate the checked-in official package registry tree."""

from __future__ import annotations

import argparse
from pathlib import Path

from gupkg.registry import validate_registry_tree


def main() -> int:
    """Validate one ``pkgs`` directory and print its selectors."""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("pkgs"))
    args = parser.parse_args()
    packages = validate_registry_tree(args.root)
    for package in packages:
        print(f"{package.selector}\t{package.version_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
