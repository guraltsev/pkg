# Cookbook

Short recipes for common administration tasks. Each one is self-contained.
Commands assume `gupkg` is on `PATH`; in scheduled tasks use its full path.

## Conventions used by every command

- **Exit status:** `0` success, `2` you or the configuration need to change
  something, `3` a change to the system failed, `4` internal error. Scripts can
  rely on these alone.
- **`--format toml`:** prints exactly one TOML document on stdout (progress text
  is suppressed) with `ok`, `changed`, `status`, `exit_code`, `warnings`, and
  `errors`, plus per-package `[[target]]` tables for `manager` commands. Place
  global options such as `--format` and `--scope` *before* the command.
- **Safe to repeat:** `install`, `update`, and `manager update` can be rerun at
  any time. Work already done is recognised and skipped.

## Install a program from GitHub Releases

Create `C:\opt\<name>\vbootstrap\pkg.toml` describing the release asset, then
run `gupkg install C:\opt\<name>`. The full walkthrough is in
[Getting started](getting_started.md). The repository's [`pkgs/`](../pkgs)
folder has ready-made definitions: copy a folder, run `gupkg install`.

## See everything at a glance

```bat
gupkg manager list                      :: every package, its versions, and its health
gupkg manager list --filter unhealthy   :: only those with problems
gupkg manager update --check-only       :: which packages have a newer release?
gupkg manager doctor                    :: validate everything; exit 2 on any problem
```

`list` and `doctor` read only the local disk and are instant. Asking upstream
about new releases is what `manager update --check-only` does.

`manager` commands need a [manager configuration](operations.md#manager-configuration)
naming your user and system package folders. `gupkg manager init` creates one
with sensible defaults, plus the folders it names.

## Update everything

```bat
gupkg manager update --check-only   :: plan only: what would change?
gupkg manager update                :: show the plan, ask, then update
gupkg manager update --yes          :: no questions (for scripts)
```

A failing package never stops the others; each package's outcome is reported
and the exit status is the worst one. Re-running after fixing a failure picks
up only what is left.

## Update unattended every night

Task Scheduler running as SYSTEM, using the full path to the shim:

```bat
schtasks /Create /TN "gupkg nightly update" /SC DAILY /ST 03:00 /RU SYSTEM ^
  /TR "C:\bin\gupkg.exe --scope system manager update --yes"
```

`--scope system` limits the run to the system package root, which an elevated
account can always write. To keep a per-user root current, create a second task
that runs as that user with `--scope user`. `gupkg` never updates anything on its
own; only a task you create does.

## Download now, switch during a maintenance window

```bat
gupkg manager update --download-only   :: stage new versions; nothing changes yet
gupkg manager update --yes             :: later: activate the staged versions
```

## Alert on problems from a script or CI job

```bat
gupkg manager doctor || echo gupkg found problems
```

For details, parse the TOML (Python 3.11+ ships a parser):

```bat
gupkg --format toml manager update --check-only > updates.toml
python -c "import tomllib; d=tomllib.load(open('updates.toml','rb')); [print(t['id'], t['installed_version'], '->', t.get('candidate_version')) for t in d.get('target', []) if t['status']=='available']"
```

## Adopt a program you already have

1. Put the files where `gupkg` expects them:
   `C:\opt\<name>\v<version>\App\...` (for example `C:\opt\notepad2\v4.2\App`).
2. `gupkg config-fix C:\opt\<name>\v<version>` writes a starter `pkg.toml`.
   If the folder contains a `_shortcuts` folder of `.lnk` files, they are imported
   into `[[shortcut]]` entries automatically.
3. Edit `pkg.toml` to add PATH entries or wrappers, run
   `gupkg config-check`, then `gupkg install`.

`config-fix` also converts older JSON-based package folders; it always keeps a
timestamped `pkg.toml.bak.*` copy of anything it replaces (`--no-backup` opts out).

## Repair a machine

```bat
gupkg install C:\opt\<name>                 :: re-create shortcuts, PATH, wrappers
gupkg install C:\opt\<name> --refresh-app   :: also re-download the program files
gupkg manager self repair                   :: repair gupkg's own command shims
```

## Roll back an update

```bat
gupkg install C:\opt\<name>\v<older-version> --allow-downgrade
```

Old versions are never deleted by `gupkg`, so you can free space by deleting old
`v...` folders yourself (never the one `current` points to).

## Install from the official registry

```bat
gupkg manager init                     :: once: configuration and folders
gupkg manager registry sync            :: fetch the catalogue (once; then cached)
gupkg manager search editor
gupkg --scope user manager install vscode
```

`manager install` requires an explicit `--scope user` or `--scope system` because
a brand-new package has no owner yet. Add `--offline` to any registry command to
use only the cached catalogue.

## Remove a package

There is no uninstall command yet. To remove one by hand: delete its Start Menu
shortcuts, the environment variables and PATH entries its `pkg.toml` declared,
the `<name>.exe` and `<name>.config.toml` wrappers in your `bin` folder, and
finally the package folder (remove the `current` junction first with `rmdir`
so only the link, not the program, is removed).
