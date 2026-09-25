# Git/Syncthing reconciliation

This directory is a version-controlled workspace configuration and safe
reconciliation tool. Git metadata remains local; Syncthing distributes the
working tree and this directory. The old automatic bootstrapper is retired and
is not a safe compatibility mode.

## Layout

```text
repo/
  .git/                 # local to this computer
  .gitbranch/           # optional local standalone clones
  .stignore             # excludes both paths
  .gitconfig/
    config              # shared, branch-neutral workspace config
    .gitignore          # ignores local checkout state
    branch              # current branch (local and ignored)
    gitconfig.py
```

The manifest has this shape:

```ini
[workspace]
    version = 1
    remote = github

[remote "github"]
    url = git@github.com:user/repository.git
```

## Commands

Initialize a new workspace with an explicit tuple:

```text
gitconfig.py init --remote github --url git@github.com:user/repository.git
```

To add gitconfig to a repository that is already cloned and already has the
named remote, use its existing credential-free URL without repeating it:

```text
gitconfig.py init --remote github
```

`init` creates the manifest only when it is absent. It adopts existing local
Git metadata and never runs `git init` again or rewrites the named remote. If
the named remote is not already configured, supply `--url`.

`init` never creates or edits `.stignore`. It scans from the repository upward
and warns if it cannot find rules that exclude local Git metadata; initialization
still completes, while `reset` waits until the exclusions are safe. For a shared
parent folder containing several repositories, the recommended rules are:

```text
(?d)(?i)**/.git
(?d)(?i)**/.git/**
(?d)(?i)**/.gitbranch
(?d)(?i)**/.gitbranch/**
```

Syncthing `#include` files, including nested includes, are expanded during this
check, so these rules may live in a shared included rules file.

The checked-out branch is the upstream branch; to use another branch, check it
out in Git. The post-checkout hook writes its name to `.gitconfig/branch`, and
Git configuration always sets `branch.<name>.merge` to
`refs/heads/<name>`.

After Syncthing reports the folder up to date, reconcile the local metadata:

```text
gitconfig.py reset
gitconfig.py reset --dry-run --format json
```

To fetch first and inspect the current local HEAD against the expected remote
branch without reconciling files or moving local refs, run:

```text
gitconfig.py fetch
```

It reports whether HEAD is equal to, ahead of, behind, or diverged from the
manifest remote branch.

Unpublished local commits never move automatically. Interactive runs explain
ambiguous states; automation must provide stable choices with
`--resolve STATE=CHOICE`.

Publishing is explicit and verifies the remote before changing shared intent:

```text
git switch feature-1
gitconfig.py publish
```

Other supported commands are `remotes show`, `hooks install`, `hooks remove`,
and `tui`. The TUI has only Fetch, Reset (reconcile), and an
Install/configure `.gitconfig` submenu. Textual is installed on first use if
it is not already available.

`.gitbranch/` is not managed by gitconfig. If you use it for ordinary local
clones, add `/.gitbranch/` to `.gitignore` and the recursive `.gitbranch`
patterns above to `.stignore`. The tool checks and suggests those exclusions,
but does not require them for reconciliation.

`--format json` writes one schema-version-1 plan to stdout. Progress and errors
go to stderr. URL credentials are redacted in text, JSON, verbose, and error
output. A pre-existing reconciliation lock always blocks; it is never stolen.

See [_docs/design.md](_docs/design.md) for the complete state table, safety
invariants, recovery-ref format, and TUI contract.
