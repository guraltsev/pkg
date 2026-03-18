#!/usr/bin/env python3
"""Synchronize repository remotes from ``.gitconfig/.gitconfig``.

This script is intended to live inside a repository-local ``.gitconfig``
directory, for example::

    my-repo/
      .git/
      .gitconfig/
        .gitconfig
        gitconfig.py

Typical usage from inside ``.gitconfig/``::

    ./gitconfig.py

What it does in automatic mode:
1. Find the surrounding Git repository.
2. Create ``.git`` if it does not exist yet.
3. Create ``.gitconfig/.gitconfig`` if it is missing.
4. Copy any existing ``remote.*`` keys from ``.git/config`` into the snapshot.
5. Ensure remotes listed in the snapshot exist in Git.
6. Fetch the selected remote.
7. Point the local branch at ``<remote>/<branch>`` and set upstream tracking.
8. Run ``git reset --mixed`` to align the index with the remote branch.

The snapshot file is the source of truth for remote definitions. Existing Git
config values are imported into it when missing, but never silently removed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "github"
TOOL_DIRNAME = ".gitconfig"
SNAPSHOT_FILENAME = ".gitconfig"


class GitConfigError(RuntimeError):
    """Raised when the repository or Git operations are invalid."""


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem locations used by the tool."""

    repo_root: Path
    git_dir: Path
    tool_dir: Path
    snapshot: Path
    git_config: Path

    @classmethod
    def discover(cls, start: Path) -> "Paths":
        """Find the repository root by walking upward from ``start``.

        Preferred detection order:
        1. the first directory containing ``.git``;
        2. if running from ``.gitconfig/`` before ``git init``, treat its parent
           as the repository root;
        3. if an ancestor contains ``.gitconfig/``, treat that ancestor as the
           repository root.
        """
        for candidate in (start, *start.parents):
            git_dir = candidate / ".git"
            if git_dir.exists():
                return cls(
                    repo_root=candidate,
                    git_dir=git_dir,
                    tool_dir=candidate / TOOL_DIRNAME,
                    snapshot=(candidate / TOOL_DIRNAME / SNAPSHOT_FILENAME),
                    git_config=git_dir / "config",
                )

            if candidate.name == TOOL_DIRNAME:
                repo_root = candidate.parent
                return cls(
                    repo_root=repo_root,
                    git_dir=repo_root / ".git",
                    tool_dir=repo_root / TOOL_DIRNAME,
                    snapshot=repo_root / TOOL_DIRNAME / SNAPSHOT_FILENAME,
                    git_config=repo_root / ".git" / "config",
                )

            tool_dir = candidate / TOOL_DIRNAME
            if tool_dir.exists():
                return cls(
                    repo_root=candidate,
                    git_dir=candidate / ".git",
                    tool_dir=tool_dir,
                    snapshot=tool_dir / SNAPSHOT_FILENAME,
                    git_config=candidate / ".git" / "config",
                )

        raise GitConfigError(
            f"Could not infer the repository root from {start}. "
            f"Run this from inside a repository, or pass --repo-root explicitly."
        )


@dataclass(frozen=True)
class RemoteSpec:
    """A remote entry declared in the snapshot file."""

    name: str
    url: str


@dataclass(frozen=True)
class CliArgs:
    """Validated command-line options."""

    remote: str | None
    branch: str
    set_remote_url: bool
    repo_root: Path | None
    auto: bool
    init: bool
    sync_snapshot: bool
    ensure_remotes: bool
    fetch: bool
    track_branch: bool
    mixed_reset: bool


@dataclass
class GitRunner:
    """Small wrapper around ``git`` commands for one repository."""

    repo_root: Path

    def run(self, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        try:
            return subprocess.run(
                cmd,
                cwd=self.repo_root,
                text=True,
                check=check,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            output = (exc.stdout or "").strip()
            message = f"Command failed in {self.repo_root}: {' '.join(cmd)}"
            if output:
                message = f"{message}\n{output}"
            raise GitConfigError(message) from exc

    def must_have_git(self) -> None:
        if shutil.which("git") is None:
            raise GitConfigError("git is not available on PATH.")

    def init(self) -> None:
        self.run("init")

    def list_remotes(self) -> list[str]:
        cp = self.run("remote", capture=True, check=False)
        if cp.returncode != 0:
            return []
        return [line.strip() for line in cp.stdout.splitlines() if line.strip()]

    def remote_url(self, remote: str) -> str | None:
        cp = self.run("remote", "get-url", remote, capture=True, check=False)
        if cp.returncode != 0:
            return None
        value = cp.stdout.strip()
        return value or None

    def has_ref(self, refname: str) -> bool:
        cp = self.run("show-ref", "--verify", "--quiet", refname, check=False)
        return cp.returncode == 0


class SnapshotConfig:
    """Read and write Git config data stored in files."""

    def __init__(self, path: Path):
        self.path = path

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def entries(self, key_pattern: str) -> list[tuple[str, str]]:
        if not self.path.exists():
            return []

        cp = subprocess.run(
            ["git", "config", "--file", str(self.path), "--null", "--get-regexp", key_pattern],
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if cp.returncode != 0:
            return []

        pairs: list[tuple[str, str]] = []
        for chunk in (part for part in cp.stdout.split("\x00") if part):
            if "\n" not in chunk:
                continue
            key, value = chunk.split("\n", 1)
            pairs.append((key.strip(), value.rstrip("\n")))
        return pairs

    def has_key(self, key: str) -> bool:
        if not self.path.exists():
            return False
        cp = subprocess.run(
            ["git", "config", "--file", str(self.path), "--get", key],
            text=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return cp.returncode == 0

    def set_if_missing(self, key: str, value: str) -> bool:
        self.ensure_exists()
        if self.has_key(key):
            return False
        subprocess.run(
            ["git", "config", "--file", str(self.path), key, value],
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    def remote_specs(self) -> dict[str, RemoteSpec]:
        remotes: dict[str, RemoteSpec] = {}
        for key, value in self.entries(r"^remote\..*\.url$"):
            parts = key.split(".")
            if len(parts) < 3 or parts[0] != "remote" or parts[-1] != "url":
                continue
            name = ".".join(parts[1:-1])
            remotes[name] = RemoteSpec(name=name, url=value)
        return remotes


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        prog="gitconfig.py",
        description="Synchronize Git remotes from .gitconfig/.gitconfig.",
    )
    parser.add_argument("--remote", help="Remote name to use.")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help=f"Branch name. Default: {DEFAULT_BRANCH}.")
    parser.add_argument(
        "--set-remote-url",
        action="store_true",
        help="Overwrite an existing remote URL when it differs from the snapshot.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Explicit repository root. By default the tool searches upward from the current directory.",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--auto", action="store_true", help="Run the full workflow. This is the default.")
    group.add_argument("--init", action="store_true", help="Only create .git when missing.")
    group.add_argument("--sync-snapshot", action="store_true", help="Only import missing remote.* keys into .gitconfig/.gitconfig.")
    group.add_argument("--ensure-remotes", action="store_true", help="Only ensure remotes from the snapshot exist in Git.")
    group.add_argument("--fetch", action="store_true", help="Only fetch the selected remote.")
    group.add_argument("--track-branch", action="store_true", help="Only update branch tracking and HEAD for the selected branch.")
    group.add_argument("--mixed-reset", action="store_true", help="Only run git reset --mixed <remote>/<branch>.")

    ns = parser.parse_args(argv)
    single_action = any(
        [ns.init, ns.sync_snapshot, ns.ensure_remotes, ns.fetch, ns.track_branch, ns.mixed_reset]
    )
    return CliArgs(
        remote=ns.remote,
        branch=ns.branch,
        set_remote_url=ns.set_remote_url,
        repo_root=ns.repo_root,
        auto=ns.auto or not single_action,
        init=ns.init,
        sync_snapshot=ns.sync_snapshot,
        ensure_remotes=ns.ensure_remotes,
        fetch=ns.fetch,
        track_branch=ns.track_branch,
        mixed_reset=ns.mixed_reset,
    )


def choose_remote(remotes: dict[str, RemoteSpec], requested: str | None) -> str | None:
    if requested:
        return requested
    if not remotes:
        return None
    if len(remotes) == 1:
        return next(iter(remotes))
    if DEFAULT_REMOTE in remotes:
        available = ", ".join(sorted(remotes))
        print(
            f"Multiple remotes found; using default '{DEFAULT_REMOTE}'. Available: {available}",
            file=sys.stderr,
        )
        return DEFAULT_REMOTE
    available = ", ".join(sorted(remotes))
    raise GitConfigError(f"Multiple remotes found. Choose one with --remote. Available: {available}")


def ensure_git_dir(paths: Paths, git: GitRunner) -> None:
    if not paths.git_dir.exists():
        git.init()


def sync_snapshot_from_git_config(paths: Paths) -> int:
    snapshot = SnapshotConfig(paths.snapshot)
    snapshot.ensure_exists()

    existing = SnapshotConfig(paths.git_config)
    added = 0
    for key, value in existing.entries(r"^remote\."):
        if snapshot.set_if_missing(key, value):
            added += 1
    return added


def ensure_remote_registered(git: GitRunner, spec: RemoteSpec, *, overwrite_url: bool) -> None:
    remotes = set(git.list_remotes())
    if spec.name not in remotes:
        git.run("remote", "add", spec.name, spec.url)
        return

    current_url = git.remote_url(spec.name)
    if current_url is None or current_url == spec.url:
        if current_url is None:
            git.run("remote", "set-url", spec.name, spec.url)
        return

    if overwrite_url:
        git.run("remote", "set-url", spec.name, spec.url)
        return

    raise GitConfigError(
        f"Remote '{spec.name}' already exists with a different URL.\n"
        f"  existing: {current_url}\n"
        f"  desired:  {spec.url}\n"
        "Use --set-remote-url to overwrite it."
    )


def ensure_remotes(git: GitRunner, remotes: Iterable[RemoteSpec], *, overwrite_url: bool) -> None:
    for spec in remotes:
        ensure_remote_registered(git, spec, overwrite_url=overwrite_url)


def fetch_remote(git: GitRunner, remote: str) -> None:
    git.run("fetch", "--prune", remote)


def track_branch(git: GitRunner, remote: str, branch: str) -> None:
    remote_ref = f"refs/remotes/{remote}/{branch}"
    local_ref = f"refs/heads/{branch}"
    if not git.has_ref(remote_ref):
        raise GitConfigError(f"Missing remote ref {remote_ref}. Fetch the remote first.")

    git.run("update-ref", local_ref, remote_ref)
    git.run("symbolic-ref", "HEAD", local_ref)
    git.run("config", f"branch.{branch}.remote", remote)
    git.run("config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def mixed_reset(git: GitRunner, remote: str, branch: str) -> None:
    git.run("reset", "--mixed", f"{remote}/{branch}")


def auto_flow(paths: Paths, git: GitRunner, args: CliArgs) -> int:
    if not paths.git_dir.exists() and not paths.snapshot.exists():
        git.init()
        return 0

    ensure_git_dir(paths, git)
    sync_snapshot_from_git_config(paths)

    remotes = SnapshotConfig(paths.snapshot).remote_specs()
    ensure_remotes(git, remotes.values(), overwrite_url=args.set_remote_url)

    selected = choose_remote(remotes, args.remote)
    if selected is None:
        return 0
    if selected not in remotes:
        raise GitConfigError(f"Remote '{selected}' is not defined in {paths.snapshot}.")

    fetch_remote(git, selected)
    track_branch(git, selected, args.branch)
    mixed_reset(git, selected, args.branch)
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    start = (args.repo_root or Path.cwd()).resolve()
    paths = Paths.discover(start)
    git = GitRunner(paths.repo_root)

    try:
        git.must_have_git()

        if args.auto:
            return auto_flow(paths, git, args)

        if args.init:
            ensure_git_dir(paths, git)
            return 0

        if args.sync_snapshot:
            ensure_git_dir(paths, git)
            sync_snapshot_from_git_config(paths)
            return 0

        if not paths.git_dir.exists():
            raise GitConfigError("No .git directory exists yet. Run --init or --auto first.")

        sync_snapshot_from_git_config(paths)
        remotes = SnapshotConfig(paths.snapshot).remote_specs()

        if args.ensure_remotes:
            ensure_remotes(git, remotes.values(), overwrite_url=args.set_remote_url)
            return 0

        selected = choose_remote(remotes, args.remote)
        if selected is None:
            raise GitConfigError(f"No remote.<name>.url entries found in {paths.snapshot}.")
        if selected not in remotes:
            raise GitConfigError(f"Remote '{selected}' is not defined in {paths.snapshot}.")

        if args.fetch:
            fetch_remote(git, selected)
            return 0
        if args.track_branch:
            track_branch(git, selected, args.branch)
            return 0
        if args.mixed_reset:
            mixed_reset(git, selected, args.branch)
            return 0

        raise GitConfigError("No action selected.")
    except GitConfigError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
