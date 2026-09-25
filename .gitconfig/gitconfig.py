#!/usr/bin/env python3
"""Safe Git/Syncthing workspace reconciliation.

The synchronized ``.gitconfig`` directory contains intent and this program;
the repository's ``.git`` directory remains local to each computer. The
implementation deliberately has a plan/resolve/execute boundary so a fetch
cannot accidentally become a branch move.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TOOL_DIRNAME = ".gitconfig"
# ``config`` is the shared, branch-neutral workspace configuration.  The
# sibling ``branch`` file is deliberately local checkout state and is ignored
# by ``.gitconfig/.gitignore``.
MANIFEST_NAME = "config"
BRANCH_NAME = "branch"
LOCAL_IGNORE_NAME = ".gitignore"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DECISION = 3
EXIT_EXTERNAL = 4
EXIT_LOCAL = 5
EXIT_GIT = 6


class GitConfigError(RuntimeError):
    """An expected user-facing failure."""

    def __init__(self, message: str, code: int = EXIT_GIT):
        super().__init__(message)
        self.code = code


def redact_url(value: str) -> str:
    """Return a safe rendering for URI, query-secret, and scp-like URLs."""
    if not value:
        return value
    if "://" not in value and re.match(r"^[^/@\s]+@[^/:\s]+:", value):
        return value.split("@", 1)[1]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return value.split("#", 1)[0]
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    sensitive = {"token", "access_token", "password", "secret", "key"}
    query = [(key, "<redacted>" if key.lower() in sensitive else item)
             for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), ""))


# Kept as a small import-level compatibility alias for callers that used the
# old helper; it does not restore the old automatic command behavior.
display_url = redact_url


def redact_text(value: str) -> str:
    """Redact common credential-bearing URL forms in Git output/errors."""
    value = re.sub(r"(?i)(https?://)([^\s/@]+)(?::[^\s/@]*)?@", r"\1<redacted>@", value)
    value = re.sub(r"(?i)([?&](?:token|access_token|password|secret|key)=)[^&\s]+", r"\1<redacted>", value)
    return re.sub(r"(?i)(https?://[^\s#]+)#\S+", r"\1", value)


def url_contains_secret(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.scheme.lower() in {"http", "https"} and "@" in parsed.netloc:
        return True
    sensitive = {"token", "access_token", "password", "secret", "key"}
    return any(key.lower() in sensitive for key, _ in parse_qsl(parsed.query, keep_blank_values=True))


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    git_dir: Path
    tool_dir: Path
    manifest: Path

    @property
    def branch_file(self) -> Path:
        return self.tool_dir / BRANCH_NAME

    @property
    def local_ignore(self) -> Path:
        return self.tool_dir / LOCAL_IGNORE_NAME

    @classmethod
    def discover(cls, start: Path, explicit: Path | None = None) -> "Paths":
        if explicit is not None:
            return cls._from_root(explicit.expanduser().resolve())
        start = start.expanduser().resolve()
        for candidate in (start, *start.parents):
            if candidate.name == TOOL_DIRNAME:
                return cls._from_root(candidate.parent)
            if (candidate / ".gitconfig").is_dir() or (candidate / ".git").exists():
                return cls._from_root(candidate)
        raise GitConfigError(f"Could not infer repository root from {start}; use --repo-root.", EXIT_USAGE)

    @classmethod
    def _from_root(cls, root: Path) -> "Paths":
        tool = root / TOOL_DIRNAME
        git = root / ".git"
        if git.is_file():
            try:
                line = git.read_text(encoding="utf-8").splitlines()[0]
                if line.lower().startswith("gitdir:"):
                    git = (git.parent / line.split(":", 1)[1].strip()).resolve()
            except (OSError, UnicodeError, IndexError):
                pass
        return cls(root, git, tool, tool / MANIFEST_NAME)


class Reporter:
    PREFIX = "[gitconfig]"

    def __init__(self, *, verbose=False, quiet=False, fmt="text"):
        self.verbose = verbose
        self.quiet = quiet
        self.fmt = fmt

    def _line(self, message: str, stream) -> None:
        print(f"{self.PREFIX} {redact_text(message)}", file=stream)

    def info(self, message: str) -> None:
        if not self.quiet and self.fmt == "text":
            self._line(message, sys.stderr)

    def detail(self, message: str) -> None:
        if self.verbose and not self.quiet and self.fmt == "text":
            self._line(message, sys.stderr)

    def warn(self, message: str) -> None:
        if self.fmt == "text":
            self._line(f"WARNING: {message}", sys.stderr)

    def plan(self, message: str) -> None:
        if self.fmt == "text":
            self._line(message, sys.stderr)

    def error(self, message: str) -> None:
        self._line(f"ERROR: {message}", sys.stderr)


@dataclass
class GitRunner:
    repo_root: Path
    reporter: Reporter

    def __post_init__(self) -> None:
        self.env = os.environ.copy()
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_TERMINAL_PROMPT"] = "0"

    def run(self, *args: str, check=True) -> subprocess.CompletedProcess[str]:
        display = " ".join(shlex.quote(redact_url(a) if ("@" in a or "://" in a) else a) for a in ("git", *args))
        self.reporter.detail(f"Running: {display}")
        try:
            cp = subprocess.run(["git", *args], cwd=self.repo_root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        except OSError as exc:
            raise GitConfigError(f"Unable to run Git: {redact_text(str(exc))}", EXIT_GIT) from exc
        output = redact_text(cp.stdout or "")
        if cp.returncode and check:
            detail = f"\n{output.strip()}" if output.strip() else ""
            raise GitConfigError(f"Git command failed: {display}{detail}", EXIT_GIT)
        if self.reporter.verbose and output.strip():
            for line in output.rstrip().splitlines():
                self.reporter.detail(f"git: {line}")
        return subprocess.CompletedProcess(cp.args, cp.returncode, output, cp.stderr)

    def value(self, *args: str) -> str:
        return (self.run(*args).stdout or "").strip()

    def optional(self, *args: str) -> tuple[int, str]:
        cp = self.run(*args, check=False)
        return cp.returncode, (cp.stdout or "").strip()

    def ref(self, ref: str) -> str | None:
        code, out = self.optional("rev-parse", "--verify", ref)
        return out.splitlines()[-1].strip() if code == 0 and out else None

    def must_have_git(self) -> None:
        if shutil.which("git") is None:
            raise GitConfigError("git is not available on PATH.", EXIT_GIT)


@dataclass(frozen=True)
class RemoteSpec:
    name: str
    url: str


@dataclass(frozen=True)
class Manifest:
    version: str
    remote: str
    remotes: dict[str, RemoteSpec]
    path: Path
    content_hash: str
    raw: bytes = field(repr=False)


_SECTION = re.compile(r'^\s*\[([^\]\s]+)(?:\s+"((?:[^"\\]|\\.)*)")?\]\s*(?:[;#].*)?$')
_KEY = re.compile(r"^([ \t]*)([A-Za-z0-9_.-]+)([ \t]*=[ \t]*)(.*?)(\r?\n)?$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace(r'\"', '"').replace(r'\\', '\\')
    return value


def _manifest_records(raw: bytes) -> list[tuple[str | None, str | None, str | None, int]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitConfigError("Manifest is not valid UTF-8.", EXIT_USAGE) from exc
    section: tuple[str, str | None] | None = None
    records = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        match = _SECTION.match(line)
        if match:
            section = (match.group(1).lower(), match.group(2))
            if section[0] in {"include", "includeif"}:
                raise GitConfigError("Manifest may not contain [include] or [includeIf].", EXIT_USAGE)
            records.append((section[0], section[1], None, number))
            continue
        match = _KEY.match(line)
        if match and section:
            records.append((section[0], section[1], f"{match.group(2).lower()}={_unquote(match.group(4))}", number))
    return records


def valid_branch(branch: str, git: GitRunner | None = None) -> bool:
    if not branch or branch.startswith("-"):
        return False
    if git:
        return git.run("check-ref-format", "--branch", branch, check=False).returncode == 0
    return bool(re.match(r"^[^\s~^:?*\\\[\x00]+$", branch)) and ".." not in branch


def valid_remote_name(name: str) -> bool:
    return bool(name) and not re.search(r"[\s:\x00-\x1f\x7f]", name) and valid_branch(name)


def parse_manifest(path: Path, git: GitRunner | None = None) -> Manifest:
    if not path.exists():
        raise GitConfigError(f"Manifest not found: {path}. Run init first.", EXIT_USAGE)
    raw = path.read_bytes()
    records = _manifest_records(raw)
    values: dict[str, list[str]] = {"version": [], "remote": []}
    remotes: dict[str, list[str]] = {}
    for section, name, item, _ in records:
        if not item:
            continue
        key, value = item.split("=", 1)
        if section == "workspace" and key in values:
            values[key].append(value.strip())
        if section == "remote" and name is not None and key == "url":
            remotes.setdefault(name, []).append(value.strip())
    for key in values:
        if len(values[key]) != 1 or not values[key][0]:
            raise GitConfigError(f"Manifest must contain exactly one nonempty workspace.{key}.", EXIT_USAGE)
    version, remote = (values[key][0] for key in ("version", "remote"))
    if version != "1":
        raise GitConfigError("Manifest workspace.version must equal 1.", EXIT_USAGE)
    if not valid_remote_name(remote):
        raise GitConfigError(f"Invalid workspace.remote name: {remote!r}.", EXIT_USAGE)
    specs: dict[str, RemoteSpec] = {}
    for name, urls in remotes.items():
        if not valid_remote_name(name):
            raise GitConfigError(f"Invalid remote name in manifest: {name!r}.", EXIT_USAGE)
        if len(urls) != 1 or not urls[0]:
            if name == remote:
                raise GitConfigError(f"Manifest remote {name!r} must contain exactly one nonempty url.", EXIT_USAGE)
            continue
        if url_contains_secret(urls[0]):
            raise GitConfigError(f"Manifest remote {name!r} contains credentials or a secret query value.", EXIT_USAGE)
        specs[name] = RemoteSpec(name, urls[0])
    if remote not in specs:
        raise GitConfigError(f"workspace.remote {remote!r} has no unique remote.{remote}.url.", EXIT_USAGE)
    return Manifest(version, remote, specs, path, hashlib.sha256(raw).hexdigest(), raw)


def _replacement_line(raw: bytes, key: str, value: str) -> bytes:
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    section: tuple[str, str | None] | None = None
    changed = False
    output: list[str] = []
    for line in lines:
        sm = _SECTION.match(line)
        if sm:
            section = (sm.group(1).lower(), sm.group(2))
        km = _KEY.match(line)
        if section == ("workspace", None) and km and km.group(2).lower() == key:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            line = f"{km.group(1)}{km.group(2)}{km.group(3)}{value}{ending}"
            changed = True
        output.append(line)
    if not changed:
        raise GitConfigError(f"Manifest key workspace.{key} was not found.", EXIT_USAGE)
    return "".join(output).encode("utf-8")


def atomic_manifest_update(path: Path, expected_hash: str, key: str, value: str) -> None:
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() != expected_hash:
        raise GitConfigError("Manifest changed while planning; shared state is blocked.", EXIT_EXTERNAL)
    updated = _replacement_line(current, key, value)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise GitConfigError(f"Unable to atomically write manifest: {exc}", EXIT_EXTERNAL) from exc


@dataclass(frozen=True)
class Choice:
    identifier: str
    label: str
    consequence: str


@dataclass(frozen=True)
class Decision:
    state: str
    reason: str
    observed: dict[str, Any]
    choices: tuple[Choice, ...]
    default: str


@dataclass(frozen=True)
class Action:
    kind: str
    description: str
    expected_old_oid: str | None = None
    new_oid: str | None = None
    ref: str | None = None


@dataclass
class Plan:
    schema_version: int = 1
    actions: list[Action] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    selected_remote_oid: str | None = None
    recovery_refs: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    fetch_side_effects: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Observation:
    repo_root: str
    manifest_remote: str
    manifest_branch: str
    manifest_hash: str
    remote_url: str
    head: str | None
    current_branch: str | None
    target_oid: str | None
    remote_oid: str | None
    ahead: int = 0
    behind: int = 0
    staged_paths: list[str] = field(default_factory=list)
    unmerged_paths: list[str] = field(default_factory=list)
    working_paths: list[str] = field(default_factory=list)
    conflict_paths: list[str] = field(default_factory=list)
    lock_paths: list[str] = field(default_factory=list)
    remote_configured: bool = False
    remote_url_local: str | None = None
    stignore_valid: bool = True


def _decision(state: str, reason: str, observed: dict[str, Any], choices: Iterable[tuple[str, str, str]], default: str) -> Decision:
    return Decision(state, reason, observed, tuple(Choice(*choice) for choice in choices), default)


def build_plan(obs: Observation) -> Plan:
    """Pure classification/planning function used by CLI and TUI adapters."""
    plan = Plan(selected_remote_oid=obs.remote_oid)
    if not obs.stignore_valid:
        plan.blocking.append(".stignore does not prove that .git is ignored")
    if obs.lock_paths:
        plan.blocking.append("existing Git/tool lock: " + ", ".join(obs.lock_paths))
    if obs.unmerged_paths:
        plan.blocking.append("unmerged index entries: " + ", ".join(obs.unmerged_paths))
    if obs.staged_paths:
        plan.blocking.append("staged or index-only changes: " + ", ".join(obs.staged_paths))
    if obs.conflict_paths:
        plan.decisions.append(_decision(
            "syncthing_conflicts", "Syncthing conflict files may represent competing edits.",
            {"paths": obs.conflict_paths},
            (("stop", "Resolve conflict files first", "No Git metadata changes"),
             ("continue_untracked", "Continue and preserve them as untracked files", "Mixed reset leaves files in place")), "stop"))
    if obs.current_branch is None:
        plan.blocking.append("HEAD is detached; check out a branch before resetting")
    if obs.remote_oid is None:
        plan.decisions.append(_decision(
            "remote_branch_missing", "The checked-out branch is absent from the fetched remote.",
            {"remote": obs.manifest_remote, "branch": obs.manifest_branch},
            (("stop", "Retry after the branch is published", "No local branch or index change"),
             ("exit", "Exit", "No local branch or index change")), "stop"))
    # A freshly initialized repository has an unborn HEAD and no commit; that
    # is a safe first-time baseline. A detached committed HEAD remains an
    # explicit ambiguity.
    if obs.target_oid is None and obs.remote_oid:
        plan.actions.extend([
            Action("create-ref", f"Create local branch {obs.manifest_branch} at remote OID", None, obs.remote_oid, f"refs/heads/{obs.manifest_branch}"),
            Action("symbolic-head", f"Point HEAD at {obs.manifest_branch}"),
            Action("mixed-reset", f"Refresh index against remote OID {obs.remote_oid}", None, obs.remote_oid),
        ])
    elif obs.target_oid and obs.remote_oid:
        if obs.ahead and obs.behind:
            plan.decisions.append(_decision(
                "history_diverged", "Local and remote histories have diverged.",
                {"local": obs.target_oid, "remote": obs.remote_oid, "ahead": obs.ahead, "behind": obs.behind},
                (("stop", "Stop for merge/rebase in a separate local clone", "No change"),
                 ("keep_local", "Keep local history unchanged", "No branch or index change"),
                 ("use_remote_baseline", "Preserve local history, then use remote baseline", "Create recovery ref and move branch")), "stop"))
        elif obs.ahead:
            plan.decisions.append(_decision(
                "history_ahead", "The local branch contains unpublished commits.",
                {"local": obs.target_oid, "remote": obs.remote_oid, "ahead": obs.ahead},
                (("stop", "Stop and resolve publication manually", "No change"),
                 ("keep_local", "Keep local history unchanged", "No branch or index change"),
                 ("use_remote_baseline", "Preserve local history, then use remote baseline", "Create recovery ref and move branch")), "stop"))
        else:
            if obs.behind:
                plan.actions.append(Action("update-ref", f"Fast-forward {obs.manifest_branch} to remote OID", obs.target_oid, obs.remote_oid, f"refs/heads/{obs.manifest_branch}"))
            plan.actions.extend([
                Action("symbolic-head", f"Point HEAD at {obs.manifest_branch}"),
                Action("mixed-reset", f"Refresh index against remote OID {obs.remote_oid}", None, obs.remote_oid),
            ])
    plan.postconditions.extend([f"HEAD is refs/heads/{obs.manifest_branch}", "index has no newly unmerged entries"])
    return plan


_STIGNORE_REQUIRED_COMPONENTS = (".git",)
_STIGNORE_RECOMMENDED_COMPONENTS = (".git", ".gitbranch")
_STIGNORE_INCLUDE = re.compile(r"^#include\s+(.+?)\s*$", re.I)


def recommended_stignore_rules(components: tuple[str, ...] = _STIGNORE_RECOMMENDED_COMPONENTS) -> tuple[str, ...]:
    """Return exclusions that protect local metadata at every tree depth."""
    return tuple(rule for component in components for rule in
                 (f"(?d)(?i)**/{component}", f"(?d)(?i)**/{component}/**"))


def stignore_instructions(components: tuple[str, ...] = _STIGNORE_RECOMMENDED_COMPONENTS) -> str:
    rules = "\n  ".join(recommended_stignore_rules(components))
    return f"Make sure your .stignore includes rules such as:\n  {rules}"


def expanded_stignore_rules(path: Path) -> list[str]:
    """Read Syncthing ignore rules, expanding nested `#include` directives."""
    visited: set[Path] = set()

    def read(current: Path) -> list[str]:
        canonical = current.resolve()
        if canonical in visited:
            raise GitConfigError(f".stignore include is repeated or cyclic: {current}", EXIT_EXTERNAL)
        visited.add(canonical)
        try:
            lines = current.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise GitConfigError(f"cannot read {current}: {exc}", EXIT_EXTERNAL) from exc
        expanded: list[str] = []
        for line in lines:
            rule = line.strip()
            include = _STIGNORE_INCLUDE.match(rule)
            if include:
                included = (current.parent / include.group(1)).resolve()
                expanded.extend(read(included))
            elif rule and not rule.startswith("#"):
                expanded.append(rule)
        return expanded

    return read(path)


def stignore_ok(root: Path, components: tuple[str, ...] = _STIGNORE_REQUIRED_COMPONENTS) -> tuple[bool, str]:
    """Check this repository and every ancestor for safe Syncthing rules.

    A Syncthing folder commonly contains several repositories, so its
    `.stignore` can live above the repository root. Root-relative rules are
    evaluated relative to the file's directory; the recursive recommended
    rules work at any depth.
    """
    found: list[Path] = []
    for owner in (root, *root.parents):
        path = owner / ".stignore"
        if not path.is_file():
            continue
        found.append(path)
        try:
            rules = set(expanded_stignore_rules(path))
        except GitConfigError as exc:
            return False, str(exc)
        relative = root.relative_to(owner).as_posix()
        prefix = "" if relative == "." else f"/{relative}"
        valid = True
        for component in components:
            literal = f"{prefix}/{component}"
            recursive = {f"(?d)(?i)**/{component}", f"(?d)(?i)**/{component}/**"}
            excluded = literal in rules or recursive.issubset(rules)
            re_included = any(rule.startswith("!" + literal) for rule in rules)
            if not excluded or re_included:
                valid = False
                break
        if valid:
            return True, ""
    if found:
        locations = ", ".join(str(path) for path in found)
        names = " and ".join(components)
        return False, f"none of the .stignore files found at {locations} excludes this repository's {names}. {stignore_instructions(components)}"
    return False, f"no .stignore was found in {root} or any parent directory. {stignore_instructions(components)}"


_CONFLICT = re.compile(r"\.sync-conflict-\d{8}-\d{6}-[^/]+", re.I)


def conflict_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.is_file() and _CONFLICT.search(path.name))


def git_locks(git_dir: Path) -> list[str]:
    return sorted(str(path) for path in git_dir.rglob("*.lock")
                  if path.is_file() and path.name != "reconcile.lock") if git_dir.exists() else []


class ReconcileLock:
    def __init__(self, git: GitRunner, command: str):
        self.git = git
        self.command = command
        self.path: Path | None = None

    def acquire(self) -> None:
        raw = self.git.value("rev-parse", "--git-path", "gitconfig/reconcile.lock")
        self.path = Path(raw)
        if not self.path.is_absolute():
            self.path = (self.git.repo_root / self.path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": 1, "pid": os.getpid(), "hostname": socket.gethostname(),
                   "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "command": self.command}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            try:
                contents = self.path.read_text(encoding="utf-8")
            except OSError:
                contents = "<lock contents unavailable>"
            raise GitConfigError(f"Reconciliation lock already exists: {self.path}\n{contents}", EXIT_LOCAL)

    def release(self) -> None:
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


def current_status(git: GitRunner) -> tuple[list[str], list[str], list[str]]:
    def paths_from(*args: str) -> list[str]:
        cp = git.run(*args)
        return [item for item in (cp.stdout or "").split("\0") if item]
    unmerged = paths_from("ls-files", "-u", "-z")
    staged = paths_from("diff", "--cached", "--name-only", "-z")
    status = paths_from("status", "--porcelain=v1", "-z", "--untracked-files=all")
    working = []
    for item in status:
        path = item[3:] if len(item) > 3 and item[2] == " " else item
        if path not in staged and path not in unmerged:
            working.append(path)
    return sorted(set(staged)), sorted(set(unmerged)), sorted(set(working))


def relation(git: GitRunner, local: str | None, remote: str | None) -> tuple[int, int]:
    if not local or not remote:
        return 0, 0
    bits = (git.run("rev-list", "--left-right", "--count", f"{local}...{remote}").stdout or "").split()
    return (int(bits[0]), int(bits[1])) if len(bits) == 2 else (0, 0)


def checked_out_branch(git: GitRunner) -> str | None:
    """Return the symbolic branch checked out in this local clone."""
    code, output = git.optional("symbolic-ref", "--quiet", "--short", "HEAD")
    branch = output.splitlines()[0].strip() if code == 0 and output else ""
    return branch if branch and valid_branch(branch, git) else None


def write_branch_file(paths: Paths, git: GitRunner) -> None:
    """Seed local branch state; subsequent changes are written by Git's hook."""
    paths.tool_dir.mkdir(parents=True, exist_ok=True)
    branch = checked_out_branch(git) or "HEAD"
    paths.branch_file.write_text(branch + "\n", encoding="utf-8", newline="\n")


def ensure_local_ignore(paths: Paths) -> None:
    """Keep checkout-only state ignored by the .gitconfig-local ignore file."""
    paths.tool_dir.mkdir(parents=True, exist_ok=True)
    expected = "# Local checkout state; managed by gitconfig.\n/branch\n"
    if not paths.local_ignore.exists():
        paths.local_ignore.write_text(expected, encoding="utf-8", newline="\n")
        return
    existing = paths.local_ignore.read_text(encoding="utf-8", errors="replace")
    if not any(line.strip() in {"branch", "/branch"} for line in existing.splitlines()):
        separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
        paths.local_ignore.write_text(existing + separator + "/branch\n", encoding="utf-8", newline="\n")


def ensure_branch_upstream(git: GitRunner, remote: str, branch: str | None) -> None:
    """Make Git's upstream branch exactly match the checked-out branch."""
    if not branch:
        raise GitConfigError("HEAD is detached; check out a branch before configuring upstream.", EXIT_LOCAL)
    git.run("config", f"branch.{branch}.remote", remote)
    git.run("config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def observe(paths: Paths, manifest: Manifest, git: GitRunner, *, stignore=True) -> Observation:
    ok, _ = stignore_ok(paths.repo_root) if stignore else (True, "")
    head_code, head_text = git.optional("symbolic-ref", "--quiet", "--short", "HEAD")
    current = head_text.splitlines()[0] if head_code == 0 and head_text else None
    if current and not valid_branch(current, git):
        current = None
    branch = current
    head_oid = git.ref("HEAD")
    target_oid = git.ref(f"refs/heads/{branch}") if branch else None
    names = (git.value("remote") or "").splitlines()
    remote_local = git.value("remote", "get-url", manifest.remote) if manifest.remote in names else None
    remote_oid = git.ref(f"refs/remotes/{manifest.remote}/{branch}") if branch else None
    staged, unmerged, working = current_status(git)
    ahead, behind = relation(git, target_oid, remote_oid)
    return Observation(str(paths.repo_root), manifest.remote, branch or "(detached)", manifest.content_hash,
                       redact_url(manifest.remotes[manifest.remote].url), head_oid, current, target_oid,
                       remote_oid, ahead, behind, staged, unmerged, working, conflict_files(paths.repo_root),
                       git_locks(paths.git_dir), remote_local is not None,
                       redact_url(remote_local) if remote_local else None, ok)


def plan_text(plan: Plan) -> str:
    lines = ["Plan:"]
    if plan.blocking:
        lines.append("  Blocking: " + "; ".join(plan.blocking))
    if plan.fetch_side_effects:
        lines.append("  Fetch side effects: " + "; ".join(plan.fetch_side_effects))
    for decision in plan.decisions:
        lines.append(f"  Decision required [{decision.state}]: {decision.reason}")
        lines.append("    Observed: " + json.dumps(decision.observed, sort_keys=True))
        for choice in decision.choices:
            suffix = " (default)" if choice.identifier == decision.default else ""
            lines.append(f"    - {choice.identifier}: {choice.label} - {choice.consequence}{suffix}")
    for action in plan.actions:
        lines.append(f"  Action: {action.description}")
    return "\n".join(lines)


def prompt_decisions(plan: Plan, *, interactive: bool, resolutions: dict[str, str]) -> dict[str, str]:
    chosen = dict(resolutions)
    for decision in plan.decisions:
        allowed = {choice.identifier for choice in decision.choices}
        if decision.state in chosen:
            if chosen[decision.state] not in allowed:
                raise GitConfigError(f"Invalid resolution {decision.state}={chosen[decision.state]}.", EXIT_USAGE)
            continue
        if not interactive:
            raise GitConfigError(plan_text(plan) + f"\nResolve with --resolve {decision.state}=<choice>.", EXIT_DECISION)
        print(f"\n[gitconfig] DECISION REQUIRED: {decision.reason}", file=sys.stderr)
        print("Observed:", file=sys.stderr)
        for key, value in decision.observed.items():
            print(f"  {key}: {value}", file=sys.stderr)
        print("Choices:", file=sys.stderr)
        for number, choice in enumerate(decision.choices, 1):
            print(f"  {number}. {choice.label} ({choice.identifier}) - {choice.consequence}", file=sys.stderr)
        while True:
            try:
                answer = input(f"Select [1-{len(decision.choices)}] (default {decision.default}): ")
            except (EOFError, KeyboardInterrupt) as exc:
                raise GitConfigError("No decision supplied; no plan was executed.", EXIT_DECISION) from exc
            answer = answer.strip()
            if not answer:
                chosen[decision.state] = decision.default
                break
            if answer.isdigit() and 1 <= int(answer) <= len(decision.choices):
                chosen[decision.state] = decision.choices[int(answer) - 1].identifier
                break
            if answer in allowed:
                chosen[decision.state] = answer
                break
            print("Invalid choice; enter a menu number or stable choice identifier.", file=sys.stderr)
    return chosen


def apply_resolution(plan: Plan, obs: Observation, choices: dict[str, str]) -> None:
    for decision in plan.decisions:
        selected = choices.get(decision.state, decision.default)
        if selected in {"stop", "exit", "keep_current", "keep_local"}:
            continue
        if decision.state == "syncthing_conflicts" and selected == "continue_untracked":
            continue
        if decision.state in {"history_ahead", "history_diverged"} and selected == "use_remote_baseline":
            if obs.target_oid and obs.remote_oid:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                encoded = obs.manifest_branch.encode("utf-8").hex()
                ref = f"refs/gitconfig/recovery/b-{encoded}/{stamp}-{secrets.token_hex(8)}"
                plan.recovery_refs.append(ref)
                # The recovery ref itself must be absent; the local OID is the
                # value being stored, not the expected old value of that ref.
                plan.actions.insert(0, Action("recovery-ref", f"Create recovery ref {ref}", None, obs.target_oid, ref))
                plan.actions.extend([Action("update-ref", f"Move {obs.manifest_branch} to remote baseline", obs.target_oid, obs.remote_oid, f"refs/heads/{obs.manifest_branch}"),
                                     Action("symbolic-head", f"Point HEAD at {obs.manifest_branch}"),
                                     Action("mixed-reset", f"Refresh index against remote OID {obs.remote_oid}", None, obs.remote_oid)])
        elif selected not in {"continue_untracked", "operate_owner"}:
            raise GitConfigError(f"Resolution {decision.state}={selected} cannot be executed.", EXIT_DECISION)


def resolution_stops(plan: Plan, choices: dict[str, str]) -> bool:
    """Return true when a selected choice intentionally performs no mutation."""
    return any(choices.get(decision.state, decision.default) in
               {"stop", "exit", "keep_current", "keep_local", "operate_owner"}
               for decision in plan.decisions)


def execute_plan(plan: Plan, obs: Observation, git: GitRunner, manifest: Manifest) -> None:
    if plan.blocking:
        raise GitConfigError("\n".join(plan.blocking), EXIT_LOCAL)
    git_dir_text = git.value("rev-parse", "--git-dir")
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = (git.repo_root / git_dir).resolve()
    locks = git_locks(git_dir)
    if locks:
        raise GitConfigError("Git lock appeared before execution: " + ", ".join(locks), EXIT_LOCAL)
    if hashlib.sha256(manifest.path.read_bytes()).hexdigest() != manifest.content_hash:
        raise GitConfigError("Manifest changed after planning; no execution was performed.", EXIT_EXTERNAL)
    branch = obs.current_branch
    if not branch:
        raise GitConfigError("HEAD is detached; check out a branch before resetting.", EXIT_LOCAL)
    if obs.remote_oid and git.ref(f"refs/remotes/{manifest.remote}/{branch}") != obs.remote_oid:
        raise GitConfigError("Fetched remote-tracking ref changed after planning; no execution was performed.", EXIT_LOCAL)
    for action in plan.actions:
        if action.kind in {"recovery-ref", "create-ref", "update-ref"}:
            current = git.ref(action.ref or "")
            expected = action.expected_old_oid
            if current != expected:
                raise GitConfigError(f"Ref changed after planning: {action.ref}", EXIT_LOCAL)
    for action in plan.actions:
        if action.kind == "recovery-ref":
            git.run("update-ref", action.ref or "", action.new_oid or "", action.expected_old_oid or "")
            if git.ref(action.ref or "") != action.new_oid:
                raise GitConfigError(f"Recovery ref verification failed: {action.ref}", EXIT_GIT)
        elif action.kind in {"create-ref", "update-ref"}:
            git.run("update-ref", action.ref or "", action.new_oid or "", action.expected_old_oid or "")
        elif action.kind == "symbolic-head":
            git.run("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        elif action.kind == "mixed-reset":
            git.run("reset", "--mixed", action.new_oid or "")
    git.run("config", f"branch.{branch}.remote", manifest.remote)
    git.run("config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    if git.ref("HEAD") != git.ref(f"refs/heads/{branch}"):
        raise GitConfigError("Postcondition failed: HEAD does not match selected branch.", EXIT_GIT)


def ensure_remote(git: GitRunner, manifest: Manifest) -> None:
    names = (git.value("remote") or "").splitlines()
    desired = manifest.remotes[manifest.remote].url
    if manifest.remote not in names:
        git.run("remote", "add", manifest.remote, desired)
        return
    existing = git.value("remote", "get-url", manifest.remote)
    if existing != desired:
        raise GitConfigError(f"Remote URL differs for {manifest.remote}: local {redact_url(existing)}, manifest {redact_url(desired)}.", EXIT_DECISION)


def parse_resolutions(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values or []:
        if "=" not in value:
            raise GitConfigError(f"Invalid --resolve value {value!r}; use STATE=CHOICE.", EXIT_USAGE)
        state, choice = value.split("=", 1)
        if not state or not choice or state in result:
            raise GitConfigError(f"Invalid or repeated --resolve value: {value!r}.", EXIT_USAGE)
        result[state] = choice
    return result


def validate_resolution_keys(plan: Plan, resolutions: dict[str, str]) -> None:
    states = {decision.state for decision in plan.decisions}
    unknown = sorted(set(resolutions) - states)
    if unknown:
        raise GitConfigError("Resolution supplied for a state not present in the plan: " + ", ".join(unknown), EXIT_USAGE)


def validate_interactive(args: argparse.Namespace) -> bool:
    terminal = sys.stdin.isatty() and sys.stderr.isatty()
    if args.interactive and not terminal:
        raise GitConfigError("--interactive requires terminal stdin and stderr.", EXIT_USAGE)
    return True if args.interactive else False if args.non_interactive else terminal


def apply_sync_overrides(manifest: Manifest, args: argparse.Namespace, git: GitRunner) -> Manifest:
    remote = args.remote or manifest.remote
    if remote not in manifest.remotes:
        raise GitConfigError(f"Remote {remote!r} is not defined in the manifest.", EXIT_USAGE)
    # The checked-out branch is always the upstream branch.  There is no
    # branch override: selecting another branch means checking it out locally.
    return dataclasses.replace(manifest, remote=remote)


def require_stignore(root: Path, args: argparse.Namespace, *, interactive: bool | None = None) -> None:
    ok, reason = stignore_ok(root)
    if ok:
        return
    if interactive is None:
        interactive = validate_interactive(args)
    if not args.allow_unverified_stignore:
        raise GitConfigError(reason, EXIT_EXTERNAL)
    if not getattr(args, "interactive", False):
        raise GitConfigError("--allow-unverified-stignore requires --interactive.", EXIT_USAGE)
    if not interactive:
        raise GitConfigError("--allow-unverified-stignore requires --interactive.", EXIT_USAGE)
    print(f"WARNING: {reason}. Continue at your own risk? [y/N] ", file=sys.stderr, end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise GitConfigError("Unverified .stignore rejected.", EXIT_EXTERNAL) from exc
    if answer != "y":
        raise GitConfigError("Unverified .stignore rejected.", EXIT_EXTERNAL)


def warn_local_clone_ignores(root: Path, git: GitRunner, reporter: Reporter) -> None:
    """Recommend, but never require, local-clone isolation under .gitbranch."""
    stignore_valid, _ = stignore_ok(root, (".gitbranch",))
    gitignore_valid = git.optional("check-ignore", "--no-index", "-q", ".gitbranch/probe")[0] == 0
    missing = []
    if not stignore_valid:
        missing.append("Syncthing .stignore")
    if not gitignore_valid:
        missing.append("Git .gitignore")
    if missing:
        reporter.warn(
            ".gitbranch is reserved for optional local clones. Consider excluding it from " +
            " and ".join(missing) + ".\n" + stignore_instructions((".gitbranch",)) +
            "\n  /.gitbranch/  # .gitignore"
        )


def command_sync(args: argparse.Namespace, reporter: Reporter) -> int:
    paths = Paths.discover(Path.cwd(), args.repo_root)
    git = GitRunner(paths.repo_root, reporter)
    git.must_have_git()
    if not paths.git_dir.exists():
        raise GitConfigError("No local Git metadata exists; run init first.", EXIT_LOCAL)
    warn_local_clone_ignores(paths.repo_root, git, reporter)
    manifest = apply_sync_overrides(parse_manifest(paths.manifest, git), args, git)
    interactive = validate_interactive(args)
    if args.allow_unverified_stignore and not interactive:
        raise GitConfigError("--allow-unverified-stignore requires --interactive.", EXIT_USAGE)
    strict = bool(args.dry_run and args.no_fetch)
    lock = None if strict else ReconcileLock(git, "sync")
    try:
        if lock:
            lock.acquire()
        # The pre-lock manifest is only for early validation. Re-read the
        # shared file after acquiring the lock so its hash and values are
        # authoritative for this plan.
        manifest = apply_sync_overrides(parse_manifest(paths.manifest, git), args, git)
        require_stignore(paths.repo_root, args, interactive=interactive)
        if not args.no_fetch:
            ensure_remote(git, manifest)
            ensure_branch_upstream(git, manifest.remote, checked_out_branch(git))
            git.run("fetch", "--prune", manifest.remote)
        obs = observe(paths, manifest, git)
        plan = build_plan(obs)
        plan.fetch_side_effects = [] if args.no_fetch else ["remote-tracking refs/FETCH_HEAD may have changed"]
        if reporter.fmt == "text":
            reporter.plan(plan_text(plan))
        resolutions = parse_resolutions(args.resolve)
        validate_resolution_keys(plan, resolutions)
        if not interactive and any(decision.state not in resolutions for decision in plan.decisions):
            if reporter.fmt == "json":
                print(json.dumps(plan.as_dict(), sort_keys=True))
            raise GitConfigError(f"{plan_text(plan)}\nResolve each state with --resolve STATE=CHOICE.", EXIT_DECISION)
        choices = prompt_decisions(plan, interactive=interactive, resolutions=resolutions)
        if resolution_stops(plan, choices):
            if reporter.fmt == "json":
                print(json.dumps(plan.as_dict(), sort_keys=True))
            return EXIT_DECISION
        apply_resolution(plan, obs, choices)
        if reporter.fmt == "json":
            print(json.dumps(plan.as_dict(), sort_keys=True))
        if args.dry_run:
            return EXIT_OK if not plan.blocking and not resolution_stops(plan, choices) else EXIT_DECISION
        execute_plan(plan, obs, git, manifest)
        reporter.info("Sync completed; working-tree files were preserved.")
        return EXIT_OK
    finally:
        if lock:
            lock.release()


def fetch_diagnosis(obs: Observation, git: GitRunner) -> dict[str, Any]:
    """Describe current HEAD against the fetched manifest remote without mutation."""
    ahead, behind = relation(git, obs.head, obs.remote_oid)
    if obs.head is None:
        status = "unborn"
    elif obs.remote_oid is None:
        status = "remote_branch_missing"
    elif ahead and behind:
        status = "diverged"
    elif behind:
        status = "behind"
    elif ahead:
        status = "ahead"
    else:
        status = "equal"
    return {
        "schema_version": 1,
        "operation": "fetch",
        "remote": obs.manifest_remote,
        "branch": obs.manifest_branch,
        "current_branch": obs.current_branch,
        "head": obs.head,
        "remote_oid": obs.remote_oid,
        "ahead": ahead,
        "behind": behind,
        "status": status,
    }


def command_fetch(args: argparse.Namespace, reporter: Reporter) -> int:
    """Fetch the selected remote and inspect the current HEAD; never reconcile."""
    paths = Paths.discover(Path.cwd(), args.repo_root)
    git = GitRunner(paths.repo_root, reporter)
    git.must_have_git()
    if not paths.git_dir.exists():
        raise GitConfigError("No local Git metadata exists; run init first.", EXIT_LOCAL)
    warn_local_clone_ignores(paths.repo_root, git, reporter)
    manifest = apply_sync_overrides(parse_manifest(paths.manifest, git), args, git)
    with ReconcileLock(git, "fetch"):
        manifest = apply_sync_overrides(parse_manifest(paths.manifest, git), args, git)
        require_stignore(paths.repo_root, args)
        ensure_remote(git, manifest)
        branch = checked_out_branch(git)
        ensure_branch_upstream(git, manifest.remote, branch)
        git.run("fetch", "--prune", manifest.remote)
        obs = observe(paths, manifest, git)
        diagnosis = fetch_diagnosis(obs, git)
    if reporter.fmt == "json":
        print(json.dumps(diagnosis, sort_keys=True))
    else:
        reporter.info(
            f"Fetched {manifest.remote}/{diagnosis['branch']}: HEAD is {diagnosis['status']} "
            f"(ahead {diagnosis['ahead']}, behind {diagnosis['behind']})."
        )
    return EXIT_OK


def create_manifest(path: Path, remote: str, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise GitConfigError(f"Manifest already exists: {path}", EXIT_USAGE)
    content = f"[workspace]\n    version = 1\n    remote = {remote}\n\n[remote \"{remote}\"]\n    url = {url}\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise GitConfigError(f"Unable to create manifest: {exc}", EXIT_EXTERNAL) from exc


def adopt_local_remote_url(git: GitRunner, remote: str | None, supplied_url: str | None) -> str | None:
    """Infer an init URL from a named local remote without changing it."""
    if not remote:
        return supplied_url
    names = (git.value("remote") or "").splitlines()
    if remote not in names:
        return supplied_url
    local_url = git.value("remote", "get-url", remote)
    if supplied_url and supplied_url != local_url:
        raise GitConfigError(
            f"Remote URL differs for {remote}: local {redact_url(local_url)}, "
            f"requested {redact_url(supplied_url)}. Refusing to create a manifest that conflicts with local Git metadata.",
            EXIT_DECISION,
        )
    return supplied_url or local_url


def command_init(args: argparse.Namespace, reporter: Reporter) -> int:
    paths = Paths.discover(Path.cwd(), args.repo_root)
    interactive = validate_interactive(args)
    remote, url = args.remote, args.url
    # ``.git`` belongs to this machine, whereas the manifest belongs to the
    # synchronized workspace. They deliberately have different lifecycles:
    # installing this tool into an already-cloned repository must create the
    # missing manifest, not reject or re-initialize that repository.
    if paths.manifest.exists():
        raise GitConfigError(f"Manifest already exists: {paths.manifest}. Use sync instead.", EXIT_USAGE)

    stignore_valid, stignore_reason = stignore_ok(paths.repo_root)

    git_exists = paths.git_dir.exists()
    git = GitRunner(paths.repo_root, reporter)
    if git_exists:
        git.must_have_git()

    # A named local remote is the authoritative source for the URL during
    # adoption. This keeps the common `init --remote github`
    # workflow credential-free and, importantly, does not overwrite an
    # existing local remote. An explicit --url remains required if that remote
    # has not been configured locally.
    if git_exists:
        url = adopt_local_remote_url(git, remote, url)

    if not (remote and url):
        if not interactive:
            missing = []
            if not remote:
                missing.append("--remote NAME")
            if not url:
                missing.append("--url URL (or an existing named local remote)")
            raise GitConfigError("init requires " + ", ".join(missing) + " in non-interactive mode.", EXIT_USAGE)
        remote = remote or input("Remote name: ").strip()
        if git_exists:
            url = adopt_local_remote_url(git, remote, url)
        url = url or input("Remote URL: ").strip()
    if not valid_remote_name(remote):
        raise GitConfigError("Invalid remote name.", EXIT_USAGE)
    if not url:
        raise GitConfigError("URL must be nonempty.", EXIT_USAGE)
    if url_contains_secret(url):
        raise GitConfigError("init URL contains credentials or a secret query value; store a credential-free URL.", EXIT_USAGE)
    if not git_exists:
        git.must_have_git()
        git.run("init")
    branch = checked_out_branch(git)
    if not branch:
        raise GitConfigError("HEAD is detached; check out a branch before installing gitconfig.", EXIT_LOCAL)
    create_manifest(paths.manifest, remote, url)
    ensure_local_ignore(paths)
    command_hooks(argparse.Namespace(repo_root=args.repo_root, hook_action="install"), reporter)
    write_branch_file(paths, git)
    ensure_branch_upstream(git, remote, branch)
    if git_exists:
        reporter.info(f"Created manifest and adopted existing local Git metadata at {paths.git_dir}.")
    else:
        reporter.info("Created manifest and initialized local Git metadata.")
    if not stignore_valid:
        reporter.warn(stignore_reason)
        warn_local_clone_ignores(paths.repo_root, git, reporter)
        return EXIT_OK
    # `resolve` belongs to the sync parser, not the init parser. Initialize it
    # explicitly so the post-bootstrap reconciliation has the same complete
    # argument shape as a direct sync invocation.
    sync_args = argparse.Namespace(**{**vars(args), "dry_run": False, "no_fetch": False, "resolve": []})
    return command_sync(sync_args, reporter)


def command_remotes(args: argparse.Namespace, reporter: Reporter) -> int:
    paths = Paths.discover(Path.cwd(), args.repo_root)
    git = GitRunner(paths.repo_root, reporter)
    manifest = parse_manifest(paths.manifest, git)
    local = {name: redact_url(git.value("remote", "get-url", name))
             for name in (git.value("remote") or "").splitlines() if name}
    safe_manifest = {name: redact_url(spec.url) for name, spec in manifest.remotes.items()}
    if getattr(args, "format", "text") == "json":
        print(json.dumps({"manifest": safe_manifest, "local": local,
                          "remote": manifest.remote}, sort_keys=True))
    else:
        print(f"remote: {manifest.remote}")
        for name, url in safe_manifest.items():
            print(f"manifest {name}: {url}")
        for name, url in local.items():
            print(f"local {name}: {url}")
    return EXIT_OK


def command_publish(args: argparse.Namespace, reporter: Reporter) -> int:
    paths = Paths.discover(Path.cwd(), args.repo_root)
    git = GitRunner(paths.repo_root, reporter)
    manifest = parse_manifest(paths.manifest, git)
    branch = checked_out_branch(git)
    if not branch:
        raise GitConfigError("HEAD is detached; check out a branch before publishing.", EXIT_LOCAL)
    if git.ref(f"refs/heads/{branch}") is None:
        raise GitConfigError(f"Local branch does not exist: {branch}", EXIT_LOCAL)
    staged, unmerged, _ = current_status(git)
    if staged or unmerged:
        raise GitConfigError("Publish requires a clean index: " + ", ".join(staged + unmerged), EXIT_LOCAL)
    interactive = validate_interactive(args)
    require_stignore(paths.repo_root, args, interactive=interactive)
    with ReconcileLock(git, "publish"):
        manifest = parse_manifest(paths.manifest, git)
        ensure_remote(git, manifest)
        ensure_branch_upstream(git, manifest.remote, branch)
        git.run("fetch", "--prune", manifest.remote)
        oid = git.ref(f"refs/heads/{branch}")
        staged, unmerged, _ = current_status(git)
        if staged or unmerged:
            raise GitConfigError("Publish requires a clean index: " + ", ".join(staged + unmerged), EXIT_LOCAL)
        git.run("push", manifest.remote, f"{oid}:refs/heads/{branch}")
        remote_code, remote_output = git.optional("ls-remote", manifest.remote, f"refs/heads/{branch}")
        observed = remote_output.split()[0] if remote_code == 0 and remote_output.split() else None
        if observed != oid:
            raise GitConfigError("Push verification failed; the checked-out branch was not published.", EXIT_EXTERNAL)
    reporter.info(f"Published {branch}; Syncthing may now distribute workspace intent.")
    return EXIT_OK


def hook_content() -> str:
    return '''#!/bin/sh
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
branch_file="$root/.gitconfig/branch"
branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || {
  printf '%s\\n' "HEAD" > "$branch_file"
  exit 0
}
printf '%s\\n' "$branch" > "$branch_file"
config="$root/.gitconfig/config"
remote=$(git config --file "$config" --get workspace.remote 2>/dev/null) || remote=
if [ -n "$remote" ]; then
  git config "branch.$branch.remote" "$remote"
  git config "branch.$branch.merge" "refs/heads/$branch"
fi
'''


def command_hooks(args: argparse.Namespace, reporter: Reporter) -> int:
    paths = Paths.discover(Path.cwd(), args.repo_root)
    git = GitRunner(paths.repo_root, reporter)
    git.must_have_git()
    hook = paths.git_dir / "hooks" / "post-checkout"
    if args.hook_action == "install":
        if hook.exists() and hook.read_text(encoding="utf-8", errors="replace") != hook_content():
            raise GitConfigError(f"Existing unrelated hook will not be overwritten: {hook}", EXIT_DECISION)
        hook.parent.mkdir(parents=True, exist_ok=True)
        ensure_local_ignore(paths)
        hook.write_text(hook_content(), encoding="utf-8", newline="\n")
        try:
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        write_branch_file(paths, git)
        reporter.info(f"Installed post-checkout branch tracker: {hook}")
    else:
        if hook.exists() and hook.read_text(encoding="utf-8", errors="replace") == hook_content():
            hook.unlink()
            reporter.info("Removed gitconfig post-checkout branch tracker.")
        else:
            raise GitConfigError("Refusing to remove an unrelated post-checkout hook.", EXIT_DECISION)
    return EXIT_OK


def install_tui_dependency() -> None:
    """Install Textual using the preferred package manager."""
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", "install", "--system", "textual"]
        display = "uv pip install --system textual"
    else:
        command = [sys.executable, "-m", "pip", "install", "--user", "textual"]
        display = f"{Path(sys.executable).name} -m pip install --user textual"

    print(f"[gitconfig] Installing the TUI dependency: {display}", file=sys.stderr)
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        raise GitConfigError(f"Unable to install the TUI dependency: {redact_text(str(exc))}", EXIT_EXTERNAL) from exc
    if completed.returncode:
        raise GitConfigError(f"TUI dependency installation failed: {display}", EXIT_EXTERNAL)


def command_tui(args: argparse.Namespace, reporter: Reporter) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()):
        raise GitConfigError("tui requires terminal stdin, stdout, and stderr; use the CLI instead.", EXIT_USAGE)
    try:
        import textual  # type: ignore  # noqa: F401
    except ImportError as exc:
        print("[gitconfig] TUI dependency unavailable. Install it now? [Y/n] ", file=sys.stderr, end="")
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt) as prompt_exc:
            raise GitConfigError("TUI dependency unavailable; installation was not approved.", EXIT_DECISION) from prompt_exc
        if answer not in {"", "y", "yes"}:
            raise GitConfigError("TUI dependency unavailable; TUI mode was not started.", EXIT_DECISION) from exc
        install_tui_dependency()
        try:
            import textual  # type: ignore  # noqa: F811
        except ImportError as import_exc:
            raise GitConfigError("TUI dependency is still unavailable after installation.", EXIT_EXTERNAL) from import_exc
    try:
        _define_tui_classes()
        return int(launch_tui(sys.modules[__name__], args, reporter))
    except GitConfigError:
        raise
    except (ImportError, OSError) as exc:
        raise GitConfigError(f"TUI frontend is unavailable: {redact_text(str(exc))}", EXIT_EXTERNAL) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitconfig.py", description="Git/Syncthing manifest reconciliation (v1).")
    def common(target: argparse.ArgumentParser, *, no_fetch: bool = True) -> None:
        target.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
        target.add_argument("--interactive", action="store_true", default=argparse.SUPPRESS)
        target.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS)
        target.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
        if no_fetch:
            target.add_argument("--no-fetch", action="store_true", default=argparse.SUPPRESS)
        target.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS)
        target.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
        target.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
        target.add_argument("--allow-unverified-stignore", action="store_true", default=argparse.SUPPRESS)
    common(parser)
    subs = parser.add_subparsers(dest="command")
    init = subs.add_parser("init")
    common(init)
    init.add_argument("--remote")
    init.add_argument("--url")
    sync = subs.add_parser("sync")
    common(sync)
    sync.add_argument("--remote")
    sync.add_argument("--resolve", action="append", default=[])
    reset = subs.add_parser("reset", help="reconcile the checked-out branch with its same-name upstream")
    common(reset)
    reset.add_argument("--remote")
    reset.add_argument("--resolve", action="append", default=[])
    fetch = subs.add_parser("fetch")
    common(fetch, no_fetch=False)
    fetch.add_argument("--remote")
    publish = subs.add_parser("publish")
    common(publish)
    remotes = subs.add_parser("remotes")
    common(remotes, no_fetch=False)
    remotes_sub = remotes.add_subparsers(dest="remote_action", required=True)
    remotes_show = remotes_sub.add_parser("show")
    common(remotes_show, no_fetch=False)
    hooks = subs.add_parser("hooks")
    common(hooks, no_fetch=False)
    hook_sub = hooks.add_subparsers(dest="hook_action", required=True)
    hooks_install = hook_sub.add_parser("install")
    common(hooks_install, no_fetch=False)
    hooks_remove = hook_sub.add_parser("remove")
    common(hooks_remove, no_fetch=False)
    tui = subs.add_parser("tui")
    tui.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    tui.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    for name, default in (("repo_root", None), ("interactive", False), ("non_interactive", False),
                          ("dry_run", False), ("no_fetch", False), ("format", "text"),
                          ("quiet", False), ("verbose", False), ("allow_unverified_stignore", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.command == "tui" and (
            args.interactive or args.non_interactive or args.dry_run or args.no_fetch or
            args.format != "text" or args.quiet or args.allow_unverified_stignore):
        parser.error("tui accepts only --repo-root and --verbose")
    if args.interactive and args.non_interactive:
        parser.error("--interactive and --non-interactive are mutually exclusive")
    if args.no_fetch and (args.command not in {"sync", "reset"} or not args.dry_run):
        parser.error("--no-fetch is valid only with reset --dry-run")
    reporter = Reporter(verbose=args.verbose, quiet=args.quiet, fmt=args.format)
    try:
        if args.command == "init":
            return command_init(args, reporter)
        if args.command in {"sync", "reset"}:
            return command_sync(args, reporter)
        if args.command == "fetch":
            return command_fetch(args, reporter)
        if args.command == "publish":
            return command_publish(args, reporter)
        if args.command == "remotes":
            return command_remotes(args, reporter)
        if args.command == "hooks":
            return command_hooks(args, reporter)
        if args.command == "tui":
            return command_tui(args, reporter)
        return EXIT_USAGE
    except GitConfigError as exc:
        reporter.error(str(exc))
        return exc.code


# =============================================================================
# TUI MODE (optional Textual frontend; intentionally kept in this file)
# =============================================================================
# The implementation is defined lazily so ordinary CLI commands do not need
# the optional Textual dependency. The repeated imports are intentional.
import argparse
import contextlib
import io
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

def _define_tui_classes() -> None:
    global App, ComposeResult, Screen, Input, Label, ListItem, ListView, Static, TuiSettings, CaptureReporter, SyncSession, _sync_args, _default_args, home_context, _short, BaseScreen, HomeScreen, ConfigureScreen, OperationScreen, EditorScreen, DecisionScreen, ResultScreen, GitConfigTui, launch_tui
    from textual.app import App, ComposeResult
    from textual.screen import Screen
    from textual.widgets import Input, Label, ListItem, ListView, Static
    @dataclass
    class TuiSettings:
        remote: str = ""
        # Kept as a session compatibility field; the simplified home screen
        # no longer exposes resolver-policy controls.
        policy: str = "ask"


    class CaptureReporter:
        """Reporter-compatible sink which never writes through Textual's terminal."""

        def __init__(self, domain: ModuleType, verbose: bool = False) -> None:
            self.domain = domain
            self.verbose = verbose
            self.quiet = False
            self.fmt = "text"
            self.lines: list[str] = []

        def _line(self, message: str, _stream: Any = None) -> None:
            self.lines.append(f"[gitconfig] {self.domain.redact_text(message)}")

        def info(self, message: str) -> None:
            self._line(message)

        def detail(self, message: str) -> None:
            if self.verbose:
                self._line(message)

        def warn(self, message: str) -> None:
            self._line(f"WARNING: {message}")

        def plan(self, message: str) -> None:
            self._line(message)

        def error(self, message: str) -> None:
            self._line(f"ERROR: {message}")


    class SyncSession:
        """A planner/executor session which keeps the reconcile lock while resolving."""

        def __init__(self, domain: ModuleType, lock: Any, obs: Any, plan: Any, manifest: Any, git: Any,
                     reporter: CaptureReporter, policy: str = "ask") -> None:
            self.domain = domain
            self.lock = lock
            self.obs = obs
            self.plan = plan
            self.manifest = manifest
            self.git = git
            self.reporter = reporter
            self.policy = policy
            self._answer = threading.Event()
            self._choices: dict[str, str] | None = None
            self.closed = False

        @classmethod
        def start(cls, domain: ModuleType, repo_root: Path, settings: TuiSettings,
                  reporter: CaptureReporter) -> "SyncSession":
            paths = domain.Paths.discover(Path.cwd(), repo_root)
            git = domain.GitRunner(paths.repo_root, reporter)
            git.must_have_git()
            if not paths.git_dir.exists():
                raise domain.GitConfigError("No local Git metadata exists; run init first.", domain.EXIT_LOCAL)

            # This first read only validates the arguments before the lock.  The
            # authoritative manifest read is deliberately repeated after acquire.
            manifest = domain.parse_manifest(paths.manifest, git)
            raw_args = _sync_args(settings, domain)
            manifest = domain.apply_sync_overrides(manifest, raw_args, git)
            lock = domain.ReconcileLock(git, "tui sync")
            lock.acquire()
            try:
                manifest = domain.apply_sync_overrides(domain.parse_manifest(paths.manifest, git), raw_args, git)
                # TUI has an explicit, visible warning for an invalid ignore file,
                # but never silently bypasses the safety check.
                domain.require_stignore(paths.repo_root, raw_args, interactive=False)
                domain.ensure_remote(git, manifest)
                domain.ensure_branch_upstream(git, manifest.remote, domain.checked_out_branch(git))
                git.run("fetch", "--prune", manifest.remote)
                obs = domain.observe(paths, manifest, git, stignore=True)
                plan = domain.build_plan(obs)
                plan.fetch_side_effects = ["remote-tracking refs/FETCH_HEAD may have changed"]
                reporter.info(f"Fetched {manifest.remote} for the checked-out branch.")
                reporter.plan(domain.plan_text(plan))
                return cls(domain, lock, obs, plan, manifest, git, reporter, settings.policy)
            except BaseException:
                lock.release()
                raise

        def submit(self, choices: dict[str, str]) -> None:
            self._choices = dict(choices)
            self._answer.set()

        def cancel(self) -> None:
            self._choices = None
            self._answer.set()

        def finish(self) -> int:
            try:
                if self._choices is None:
                    return self.domain.EXIT_DECISION
                choices = self._choices
                self.domain.validate_resolution_keys(self.plan, choices)
                self.domain.apply_resolution(self.plan, self.obs, choices)
                if self.domain.resolution_stops(self.plan, choices):
                    return self.domain.EXIT_DECISION
                self.domain.execute_plan(self.plan, self.obs, self.git, self.manifest)
                self.reporter.info("Sync completed; working-tree files were preserved.")
                return self.domain.EXIT_OK
            finally:
                self.close()

        def wait_for_choices(self) -> None:
            if self.plan.decisions:
                self._answer.wait()

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self.lock.release()


    def _sync_args(settings: TuiSettings, domain: ModuleType) -> argparse.Namespace:
        return argparse.Namespace(
            remote=settings.remote, allow_unverified_stignore=False,
            interactive=False, non_interactive=True, dry_run=False, no_fetch=False,
            format="text", quiet=True, verbose=False,
        )


    def _default_args(domain: ModuleType, **values: Any) -> argparse.Namespace:
        defaults = dict(repo_root=None, interactive=False, non_interactive=True,
                        dry_run=False, no_fetch=False, format="text", quiet=True,
                        verbose=False, allow_unverified_stignore=False)
        defaults.update(values)
        return argparse.Namespace(**defaults)


    def home_context(domain: ModuleType, repo_root: Path | None) -> tuple[list[str], dict[str, str], Any]:
        paths = domain.Paths.discover(Path.cwd(), repo_root)
        lines = [f"Repository: {paths.repo_root}"]
        settings = {"remote": "origin", "url": "", "current_branch": "main"}
        manifest = None
        try:
            reporter = CaptureReporter(domain)
            git = domain.GitRunner(paths.repo_root, reporter)
            if not paths.git_dir.exists():
                lines.append("Manifest: unavailable")
                lines.append("Warning: No local Git metadata; initialize this workspace first.")
                settings.update(remote="origin", current_branch="main")
                return lines, settings, manifest

            head = git.ref("HEAD")
            current_code, current_text = git.optional("symbolic-ref", "--quiet", "--short", "HEAD")
            current = current_text.splitlines()[0] if current_code == 0 and current_text else "detached"
            settings["current_branch"] = current if current != "detached" else "main"
            lines.append(f"HEAD: {current} ({_short(head) if head else 'unborn'})")

            try:
                manifest = domain.parse_manifest(paths.manifest, git)
            except domain.GitConfigError as exc:
                lines.append("Manifest: unavailable")
                lines.append("Warning: " + str(exc))
                names = (git.value("remote") or "").splitlines()
                if names:
                    settings["remote"] = names[0]
                    # `url` is an operation value, not display text. Keeping
                    # the raw value here prevents a redacted SSH URL such as
                    # github.com:owner/repo from being compared with its real
                    # git@github.com:owner/repo local remote during init.
                    settings["url"] = git.value("remote", "get-url", names[0])
            else:
                settings.update(remote=manifest.remote,
                                url=manifest.remotes[manifest.remote].url)
                lines.insert(1, f"Config: {manifest.remote}")

            staged, unmerged, _working = domain.current_status(git)
            ok, reason = domain.stignore_ok(paths.repo_root)
            warnings = []
            if unmerged:
                warnings.append("Warning: unmerged index entries: " + ", ".join(unmerged))
            if staged:
                warnings.append("Warning: staged/index-only changes: " + ", ".join(staged))
            conflicts = domain.conflict_files(paths.repo_root)
            if conflicts:
                warnings.append("Warning: Syncthing conflict files: " + ", ".join(conflicts))
            if not ok:
                warnings.append("Warning: " + reason)
            lock_paths = domain.git_locks(paths.git_dir)
            lock_code, lock_text = git.optional("rev-parse", "--git-path", "gitconfig/reconcile.lock")
            if lock_code == 0 and lock_text:
                tool_lock = Path(lock_text)
                if not tool_lock.is_absolute():
                    tool_lock = (paths.repo_root / tool_lock).resolve()
                if tool_lock.is_file():
                    lock_paths.append(str(tool_lock))
            if lock_paths:
                warnings.append("Warning: lock: " + ", ".join(sorted(set(lock_paths))))
            lines.extend(warnings)
        except domain.GitConfigError as exc:
            lines.append("Warning: " + str(exc))
        return lines, settings, manifest


    def _short(value: str | None) -> str:
        return value[:12] if value else "unborn"


    class BaseScreen(Screen):
        BINDINGS = [("q", "quit_app", "Quit"), ("escape", "back", "Back")]

        def action_quit_app(self) -> None:
            self.app.exit()

        def action_back(self) -> None:
            if isinstance(self, HomeScreen):
                self.app.exit()
            elif len(self.app.screen_stack) > 1:
                self.app.pop_screen()
                if isinstance(self.app.screen, HomeScreen) and hasattr(self.app, "refresh_home"):
                    self.app.refresh_home()
            else:
                self.app.exit()


    class HomeScreen(BaseScreen):
        def __init__(self, app: "GitConfigTui", lines: list[str], settings: dict[str, str]) -> None:
            super().__init__()
            self.tui = app
            self.lines = lines
            self.settings = settings

        def compose(self) -> ComposeResult:
            yield Static(self.tui.render_home_context(), id="context")
            rows = [
                ListItem(Label("Fetch"), id="home-fetch"),
                ListItem(Label("Reset (reconcile)"), id="home-reset"),
                ListItem(Label("Install/configure .gitconfig"), id="home-config"),
                ListItem(Label("Exit"), id="home-exit"),
            ]
            yield ListView(*rows, id="home-list")

        def on_mount(self) -> None:
            menu = self.query_one("#home-list", ListView)
            menu.focus()
            menu.index = 0

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if event.item.id == "home-fetch":
                self.tui.fetch_from_home()
                return
            actions = {"home-reset": "sync"}
            action = actions.get(event.item.id or "")
            if action:
                self.tui.open_operation(action, self.settings)
            elif event.item.id == "home-config":
                self.tui.open_configure()
            elif event.item.id == "home-exit":
                self.app.exit()


    class ConfigureScreen(BaseScreen):
        """Small configuration submenu kept separate from daily operations."""

        def __init__(self, tui: "GitConfigTui") -> None:
            super().__init__()
            self.tui = tui

        def compose(self) -> ComposeResult:
            yield Static("Install/configure .gitconfig\nShared config and local checkout hook", id="context")
            yield ListView(
                ListItem(Label("Install/configure shared config"), id="config-manifest"),
                ListItem(Label("Install/update branch hook"), id="config-hook"),
                ListItem(Label("Back"), id="config-back"),
                id="config-list",
            )

        def on_mount(self) -> None:
            menu = self.query_one("#config-list", ListView)
            menu.focus()
            menu.index = 0

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if event.item.id == "config-manifest":
                self.tui.open_operation("init", self.tui.home_settings)
            elif event.item.id == "config-hook":
                self.tui.start_direct_operation("hooks", {"hook_action": "install"})
            elif event.item.id == "config-back":
                self.app.pop_screen()


    class OperationScreen(BaseScreen):
        def __init__(self, tui: "GitConfigTui", operation: str, initial: dict[str, str]) -> None:
            super().__init__()
            self.tui = tui
            self.operation = operation
            self.values: dict[str, Any] = dict(initial)
            self.list_view: ListView | None = None
            self.row_index = 0

        def _title(self) -> str:
            return {"init": "Initialize workspace", "sync": "Reconcile synchronized files",
                    "publish": "Publish current branch", "remotes": "View configured remotes",
                    "hooks": "Manage checkout notifier"}.get(
                        self.operation, self.operation)

        def _settings(self) -> list[tuple[str, str, str]]:
            if self.operation == "init":
                return [("remote", "Remote name", str(self.values.get("remote", ""))),
                        # Keep the exact URL in `values` for init, but never
                        # render its userinfo or secret query values in the UI.
                        ("url", "Remote URL", self.tui.domain.redact_url(str(self.values.get("url", ""))))]
            return []

        def compose(self) -> ComposeResult:
            descriptions = {
                "init": "Create or configure the shared branch-neutral config.",
                "sync": "Reset the checked-out branch to its same-name upstream; files are preserved.",
                "publish": "Send the checked-out branch to the configured remote.",
                "remotes": "Compare the shared config remotes with local Git remotes.",
                "hooks": "Install the local hook that records the checked-out branch.",
            }
            yield Static(f"{self._title()}\n{descriptions[self.operation]}", id="context")
            self.list_view = ListView(*self._rows(), id="operation-list")
            yield self.list_view

        def on_mount(self) -> None:
            assert self.list_view is not None
            self.list_view.focus()
            self.list_view.index = self.row_index

        def _rows(self) -> list[ListItem]:
            action_label = {"init": "Install/configure .gitconfig",
                            "sync": "Reset (reconcile)", "publish": "Publish branch",
                            "remotes": "Show remotes", "hooks": "Apply hook action"}[self.operation]
            action = ListItem(Label(action_label), id="operation-action")
            action._tui_kind = "action"  # type: ignore[attr-defined]
            rows = [action]
            settings = self._settings()
            if settings:
                separator = ListItem(Label("--- Settings ---"), id="settings-separator", disabled=True)
                rows.append(separator)
            for key, label, value in settings:
                row = ListItem(Label(f"{label}: {value}"), id=f"setting-{key}")
                row._tui_kind = "setting"  # type: ignore[attr-defined]
                row._tui_setting = key  # type: ignore[attr-defined]
                rows.append(row)
            return rows

        def refresh_rows(self) -> None:
            if self.list_view is None:
                return
            self.list_view.clear()
            self.list_view.mount(*self._rows())
            self.app.call_after_refresh(self._restore_index)

        def _restore_index(self) -> None:
            if self.list_view is not None:
                self.list_view.index = self.row_index

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item = event.item
            self.row_index = self.list_view.index if self.list_view else 0
            if getattr(item, "_tui_kind", None) == "action":
                self.tui.start_operation(self.operation, self.values, self)
                return
            key = getattr(item, "_tui_setting", None)
            if key:
                self.edit_setting(key)

        def edit_setting(self, key: str) -> None:
            if key == "hook_action":
                self.values[key] = "remove" if self.values.get(key) == "install" else "install"
                self.refresh_rows()
                if self.list_view:
                    self.list_view.index = self.row_index
            else:
                current = str(self.values.get(key, ""))
                self.app.push_screen(EditorScreen(f"Edit {key}", current, self._validate(key)),
                                      lambda value: self._edited(key, value))

        def _validate(self, key: str) -> Callable[[str], str | None]:
            if key == "remote":
                if self.operation == "init":
                    return lambda value: None if self.domain_valid_remote(value.strip()) else "Invalid remote name."
                return lambda value: None if self.tui.valid_remote(value.strip()) else "Remote must be a defined manifest remote."
            if key == "url":
                return lambda value: None if value.strip() and not self.tui.domain.url_contains_secret(value.strip()) else "Enter a nonempty URL without credentials or secret query values."
            if key == "branch":
                return lambda value: None if self.tui.valid_branch(value.strip()) else "Invalid Git branch name."
            return lambda _value: None

        def domain_valid_remote(self, value: str) -> bool:
            return self.tui.domain.valid_remote_name(value)

        def _edited(self, key: str, value: str | None) -> None:
            if value is not None:
                self.values[key] = value
                self.refresh_rows()
                if self.list_view:
                    self.list_view.index = self.row_index


    class EditorScreen(BaseScreen):
        def __init__(self, title: str, value: str, validator: Callable[[str], str | None]) -> None:
            super().__init__()
            self.title = title
            self.value = value
            self.validator = validator
            self.error: Static | None = None

        def compose(self) -> ComposeResult:
            yield Static(self.title, id="editor-context")
            yield Input(value=self.value, id="editor-input")
            yield Static("", id="editor-error")

        def on_mount(self) -> None:
            self.query_one("#editor-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            value = event.value.strip()
            error = self.validator(value)
            if error:
                self.query_one("#editor-error", Static).update(error)
                return
            self.dismiss(value)

        def on_key(self, event: Any) -> None:
            if event.key == "q":
                event.stop()
                self.app.exit()
            elif event.key == "escape":
                event.stop()
                self.dismiss(None)

        def action_back(self) -> None:
            self.dismiss(None)


    class DecisionScreen(BaseScreen):
        def __init__(self, tui: "GitConfigTui", session: SyncSession) -> None:
            super().__init__()
            self.tui = tui
            self.session = session
            self.decision_index = 0
            self.choices: dict[str, str] = {}
            self.list_view: ListView | None = None

        @property
        def decision(self) -> Any:
            return self.session.plan.decisions[self.decision_index]

        def compose(self) -> ComposeResult:
            decision = self.decision
            observed = "\n".join(f"{key}: {value}" for key, value in decision.observed.items())
            effects = "\nFetch side effects: " + "; ".join(self.session.plan.fetch_side_effects) if self.session.plan.fetch_side_effects else ""
            yield Static(f"[gitconfig] DECISION REQUIRED: {decision.reason}\n"
                         f"Why this needs a decision: {decision.reason}\nObserved:\n{observed}{effects}\nChoices:", id="context")
            self.list_view = ListView(*self._rows(), id="decision-list")
            yield self.list_view

        def on_mount(self) -> None:
            assert self.list_view is not None
            self.list_view.focus()
            self.list_view.index = 0

        def _rows(self) -> list[ListItem]:
            decision = self.decision
            rows = []
            for number, choice in enumerate(decision.choices, 1):
                suffix = " (default)" if choice.identifier == decision.default else ""
                disabled = self.session.policy == "safe only" and choice.identifier != decision.default
                availability = " (unavailable under safe-only policy)" if disabled else ""
                row = ListItem(Label(f"{number}. {choice.label} - {choice.consequence}{suffix}{availability}"),
                               id=f"choice-{choice.identifier}", disabled=disabled)
                row._tui_choice = choice.identifier  # type: ignore[attr-defined]
                row._tui_state = decision.state  # type: ignore[attr-defined]
                rows.append(row)
            return rows

        def refresh_rows(self) -> None:
            if self.list_view is None:
                return
            self.list_view.clear()
            self.list_view.mount(*self._rows())
            self.app.call_after_refresh(self._restore_index)

        def _restore_index(self) -> None:
            if self.list_view is not None:
                self.list_view.index = 0

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            choice = getattr(event.item, "_tui_choice", None)
            if choice is None:
                return
            self.choices[self.decision.state] = choice
            if self.decision_index + 1 < len(self.session.plan.decisions):
                self.decision_index += 1
                self.refresh_context()
                self.refresh_rows()
                if self.list_view:
                    self.list_view.index = 0
            else:
                self.tui.submit_sync_decisions(self.session, self.choices)

        def action_back(self) -> None:
            self.session.cancel()
            if len(self.app.screen_stack) > 1:
                self.app.pop_screen()

        def refresh_context(self) -> None:
            decision = self.decision
            observed = "\n".join(f"{key}: {value}" for key, value in decision.observed.items())
            effects = "\nFetch side effects: " + "; ".join(self.session.plan.fetch_side_effects) if self.session.plan.fetch_side_effects else ""
            self.query_one("#context", Static).update(
                f"[gitconfig] DECISION REQUIRED: {decision.reason}\n"
                f"Why this needs a decision: {decision.reason}\nObserved:\n{observed}{effects}\nChoices:"
            )


    class ResultScreen(BaseScreen):
        def __init__(self, tui: "GitConfigTui", title: str) -> None:
            super().__init__()
            self.tui = tui
            self.title = title
            self.status = "Running"
            self.output = ""

        def compose(self) -> ComposeResult:
            yield Static(self.title, id="result-command")
            yield Static(self.status, id="result-status")
            yield Static(self.output, id="result-output")

        def set_result(self, status: str, output: str) -> None:
            self.status = status
            self.output = output
            self.query_one("#result-status", Static).update(status)
            self.query_one("#result-output", Static).update(output)

        def action_back(self) -> None:
            self.tui.leave_result()


    class GitConfigTui(App):
        CSS = """
        Screen { padding: 0 1; }
        #context, #editor-context, #result-command, #result-status { height: auto; width: 1fr; }
        #result-output { height: 1fr; width: 1fr; overflow-y: scroll; }
        #home-list, #operation-list, #decision-list { height: 1fr; width: 1fr; }
        #editor-input { height: 1; border: none; width: 1fr; }
        #editor-error { color: yellow; height: auto; }
        ListItem { height: auto; padding: 0; }
        ListItem > Label { height: 1; text-overflow: ellipsis; }
        ListItem.-disabled { color: grey; }
        """
        TITLE = "gitconfig"

        def __init__(self, domain: ModuleType, args: argparse.Namespace, reporter: Any) -> None:
            super().__init__()
            self.domain = domain
            self.args = args
            self.reporter = reporter
            self.repo_root = args.repo_root
            self.home_lines, self.home_settings, self.manifest = home_context(domain, self.repo_root)
            self.fetch_status = "Fetch to compare with upstream"
            self.fetch_in_progress = False
            self.origin: OperationScreen | None = None
            self.active_result: ResultScreen | None = None
            self.active_session: SyncSession | None = None
            self.active_worker: Any = None
            self._closed = False

        def compose(self) -> ComposeResult:
            # HomeScreen is a Screen, not a child widget.  It is pushed in
            # on_mount so Textual has a real active screen to render.
            yield from ()

        def on_mount(self) -> None:
            self.push_screen(HomeScreen(self, self.home_lines, self.home_settings))

        def render_home_context(self) -> str:
            return "\n".join([*self.home_lines, f"Upstream: {self.fetch_status}"])

        def update_home_context(self) -> None:
            home = next((screen for screen in reversed(self.screen_stack)
                         if isinstance(screen, HomeScreen)), None)
            if home is not None:
                home.query_one("#context", Static).update(self.render_home_context())

        def refresh_home(self) -> None:
            lines, settings, manifest = home_context(self.domain, self.repo_root)
            self.home_lines, self.home_settings, self.manifest = lines, settings, manifest
            home = next((screen for screen in reversed(self.screen_stack)
                         if isinstance(screen, HomeScreen)), None)
            if home is not None:
                home.lines, home.settings = lines, settings
                home.query_one("#context", Static).update(self.render_home_context())

        def fetch_from_home(self) -> None:
            if self.fetch_in_progress:
                return
            self.fetch_in_progress = True
            self.fetch_status = "Fetching upstream…"
            self.update_home_context()
            self.active_worker = self.run_worker(self._home_fetch_worker, thread=True)

        def _home_fetch_worker(self) -> None:
            capture = CaptureReporter(self.domain, bool(self.args.verbose))
            try:
                args = _default_args(
                    self.domain, repo_root=self.repo_root,
                    remote=self.home_settings.get("remote", ""),
                    branch=self.home_settings.get("branch", ""),
                )
                code = self.domain.command_fetch(args, capture)
            except self.domain.GitConfigError as exc:
                capture.error(str(exc))
                code = exc.code
            except BaseException as exc:
                capture.error(str(exc))
                code = self.domain.EXIT_EXTERNAL
            self.call_from_thread(self._home_fetch_finished, code, capture.lines)

        def _home_fetch_finished(self, code: int, lines: list[str]) -> None:
            self.fetch_in_progress = False
            result = next((line for line in reversed(lines) if "Fetched " in line), None)
            if code == self.domain.EXIT_OK and result:
                self.fetch_status = result.removeprefix("[gitconfig] ")
            elif code == self.domain.EXIT_OK:
                self.fetch_status = "Fetched; no upstream comparison was reported."
            else:
                detail = lines[-1].removeprefix("[gitconfig] ERROR: ") if lines else "unknown error"
                self.fetch_status = f"Fetch failed (exit {code}): {detail}"
            self.update_home_context()

        def valid_branch(self, value: str) -> bool:
            try:
                git = self.domain.GitRunner(self.domain.Paths.discover(Path.cwd(), self.repo_root).repo_root,
                                            CaptureReporter(self.domain))
                return self.domain.valid_branch(value, git)
            except self.domain.GitConfigError:
                return self.domain.valid_branch(value)

        def open_operation(self, operation: str, home_settings: dict[str, str]) -> None:
            values: dict[str, Any] = {}
            if operation == "init":
                values = {"remote": home_settings.get("remote", "origin"),
                          "url": home_settings.get("url", "")}
            elif operation == "sync":
                values = {"remote": home_settings.get("remote", "")}
            screen = OperationScreen(self, operation, values)
            self.push_screen(screen)

        def open_configure(self) -> None:
            self.push_screen(ConfigureScreen(self))

        def start_direct_operation(self, operation: str, values: dict[str, Any]) -> None:
            result = ResultScreen(self, self._command_summary(operation, values))
            self.active_result = result
            self.push_screen(result)
            self.active_worker = self.run_worker(
                lambda: self._command_worker(operation, values, result), thread=True)

        def start_operation(self, operation: str, values: dict[str, Any], origin: OperationScreen) -> None:
            self.origin = origin
            title = self._command_summary(operation, values)
            result = ResultScreen(self, title)
            self.active_result = result
            self.push_screen(result)
            if operation == "sync":
                self.active_worker = self.run_worker(lambda: self._sync_worker(values, result), thread=True)
            else:
                self.active_worker = self.run_worker(lambda: self._command_worker(operation, values, result), thread=True)

        def _command_summary(self, operation: str, values: dict[str, Any]) -> str:
            if operation == "init":
                return f"gitconfig.py init --remote {values.get('remote', '')}"
            if operation == "sync":
                return "gitconfig.py reset"
            if operation == "publish":
                return "gitconfig.py publish"
            if operation == "hooks":
                return f"gitconfig.py hooks {values.get('hook_action', 'install')}"
            return "gitconfig.py remotes show"

        def _sync_worker(self, values: dict[str, Any], result: ResultScreen) -> None:
            capture = CaptureReporter(self.domain, bool(self.args.verbose))
            session: SyncSession | None = None
            try:
                session = SyncSession.start(self.domain, self.repo_root, TuiSettings(**values), capture)
                self.active_session = session
                self.call_from_thread(self._sync_plan_ready, session, "\n".join(capture.lines), result)
                if session.plan.decisions:
                    session.wait_for_choices()
                else:
                    session.submit({})
                code = session.finish()
                output = "\n".join(capture.lines)
                self.call_from_thread(self._operation_finished, code, output, result)
            except self.domain.GitConfigError as exc:
                if session is not None:
                    session.close()
                capture.error(str(exc))
                self.call_from_thread(self._operation_finished, exc.code, "\n".join(capture.lines), result)
            except BaseException as exc:
                if session is not None:
                    session.close()
                capture.error(str(exc))
                self.call_from_thread(self._operation_finished, self.domain.EXIT_EXTERNAL,
                                      "\n".join(capture.lines), result)

        def _sync_plan_ready(self, session: SyncSession, output: str, result: ResultScreen) -> None:
            if result not in self.screen_stack:
                session.cancel()
                return
            if session.plan.decisions:
                if result in self.screen_stack:
                    self.pop_screen()
                self.push_screen(DecisionScreen(self, session))
            elif result in self.screen_stack:
                result.set_result("Running", output or "Plan is unambiguous; executing.")

        def submit_sync_decisions(self, session: SyncSession, choices: dict[str, str]) -> None:
            if self.screen is not self.active_result:
                self.pop_screen()
            if self.active_result is not None and self.active_result not in self.screen_stack:
                self.push_screen(self.active_result)
            session.submit(choices)

        def _command_worker(self, operation: str, values: dict[str, Any], result: ResultScreen) -> None:
            capture = CaptureReporter(self.domain, bool(self.args.verbose))
            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
                    if operation == "init":
                        code = self.domain.command_init(
                            _default_args(self.domain, repo_root=self.repo_root, remote=values["remote"],
                                          url=values["url"]), capture)
                    elif operation == "remotes":
                        code = self.domain.command_remotes(
                            _default_args(self.domain, repo_root=self.repo_root, remote_action="show"), capture)
                    else:
                        code = self.domain.command_hooks(
                            _default_args(self.domain, repo_root=self.repo_root,
                                          hook_action=values["hook_action"]), capture)
            except self.domain.GitConfigError as exc:
                capture.error(str(exc))
                code = exc.code
            except BaseException as exc:
                capture.error(str(exc))
                code = self.domain.EXIT_EXTERNAL
            output = "\n".join(capture.lines + ([stdout.getvalue().strip()] if stdout.getvalue().strip() else []))
            self.call_from_thread(self._operation_finished, code, output, result)

        def _operation_finished(self, code: int, output: str, result: ResultScreen) -> None:
            if result in self.screen_stack:
                result.set_result("Completed" if code == self.domain.EXIT_OK else f"Failed (exit {code})", output)
            self.active_session = None

        def leave_result(self) -> None:
            if self.active_result is not None and self.active_result in self.screen_stack:
                self.pop_screen()

        def _cancel_active_session(self) -> None:
            self._closed = True
            if self.active_session is not None:
                self.active_session.cancel()

        def on_exit(self) -> None:
            self._cancel_active_session()

        def on_unmount(self) -> None:
            self._cancel_active_session()


    def launch_tui(domain: ModuleType, args: argparse.Namespace, reporter: Any) -> int:
        """Run the full-screen frontend and return its application exit code."""
        app = GitConfigTui(domain, args, reporter)
        app.run()
        return int(app.return_value or domain.EXIT_OK)
# =============================================================================
# SCRIPT ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    raise SystemExit(main())
