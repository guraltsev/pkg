#!/usr/bin/env python3
"""
Initialize .git in the current directory (if absent), add remote 'github', fetch,
and set local 'main' to track 'github/main' WITHOUT modifying the working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REMOTES = {
    "url": "git@github.com:guraltsev/pkg.git",
}

def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

def main() -> None:
    url = REMOTES.get("url")
    if not url:
        raise SystemExit("REMOTES['url'] is missing or empty.")

    if Path(".git").exists():
        return

    run(["git", "init"])
    run(["git", "remote", "add", "github", url])
    run(["git", "fetch", "--prune", "github"])

    # Make refs/heads/main point to refs/remotes/github/main (no checkout, no reset).
    run(["git", "update-ref", "refs/heads/main", "refs/remotes/github/main"])
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"])

    # Record upstream tracking in config (also does not touch the working tree).
    run(["git", "config", "branch.main.remote", "github"])
    run(["git", "config", "branch.main.merge", "refs/heads/main"])

if __name__ == "__main__":
    main()