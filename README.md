# gupkg

**Install and update Windows programs from a small text file, with nothing hidden.**

`gupkg` manages self-contained applications that live in ordinary folders. You
describe a program once in a short `pkg.toml`; `gupkg` downloads and verifies
it, then creates the Start Menu shortcuts, environment variables, PATH entries,
and command-line wrappers it needs. Every version stays side by side, updates
are staged before they switch over, and any command can be repeated safely to
repair a machine.

```bat
gupkg install C:\opt\pandoc            :: download, verify, install, wire up Windows
gupkg update C:\opt\pandoc             :: get the newest release; the old one stays
gupkg manager update --yes             :: do it for every package you manage
gupkg install C:\opt\pandoc\v3.6.1 --allow-downgrade    :: roll back
```

**Why use it**

- **Minimal friction.** One folder per program, one readable file per folder.
  No installers to click through, no registry archaeology.
- **Safe by construction.** Downloads are checksummed and built in a separate
  folder; the working version is never modified in place. A failed update
  changes nothing.
- **Repairable.** `install` re-applies shortcuts, PATH, and wrappers without
  re-downloading. `config-check` and `manager doctor` find problems early.
- **Built for administration.** Bulk updates with a plan and confirmation,
  `--yes` for unattended runs, stable exit codes, and `--format toml` for
  scripts. A failing package never blocks the others.
- **Transparent.** Plain files and folders, an NTFS junction, standard
  registry values. Nothing runs in the background.

## Documentation

| I want to... | Read |
| --- | --- |
| Install my first program in five minutes | [Getting started](docs/getting_started.md) |
| Do a specific task (nightly updates, rollback, CI checks, ...) | [Cookbook](docs/cookbook.md) |
| Fix an error message | [Troubleshooting](docs/troubleshooting.md) |
| Set up and run the multi-package manager, registry, self-repair | [Operations guide](docs/operations.md) |
| Look up every `pkg.toml` field | [`pkg.toml` reference](#pkgtoml-reference) below |
| Look up every command and option | `gupkg --help` and `gupkg <command> --help`, or [Commands and options](#commands-and-options) below |
| Understand or change the code | [Development guide](docs/development_guide.md) |

Requires Windows and Python 3.11 or newer (the release bundle includes its own
runtime). Git is needed only for Git-based packages; network access only when a
package contacts its source.

## Installing gupkg

`gupkg` is a self-contained tool that is deployed as a folder; it is not distributed as a
package on a Python package index. Pick the route that fits:

- **Bootstrap script (recommended).** Download `gupkg-bootstrap.ps1` and
  `gupkg-bootstrap.cmd` from this repository, put them in one folder, and run

  ```bat
  gupkg-bootstrap.cmd                  :: for you, no elevation needed
  gupkg-bootstrap.cmd -Scope system    :: for all users, from an elevated shell
  ```

  It downloads gupkg, places it at `<root>\gupkg\v<version>` (default root
  `%USERPROFILE%\opt` for user scope, `C:\opt` for system scope; `-Root` changes
  it), creates the `gupkg` and `gupkg-tui` commands and their PATH entry, and
  runs `gupkg manager init`. It is safe to repeat. Use `-Source` with a ZIP
  (URL or path) or folder, and `-Sha256` to verify it, to install a vetted
  release bundle from your own share; `-SkipInstall` only places the files.
  Nothing needs to be installed first: gupkg fetches a verified embedded
  Python itself when the machine has none.
- **Release bundle by hand.** The bundle built by `tools\build_standalone.py`
  already contains the embedded runtime and native shims; unpack it and run its
  `gupkg.cmd --scope user install <folder>`, or give it to the bootstrap script
  as `-Source`.
- **From a source checkout.** Copy the `src` folder to a place such as
  `C:\opt\gupkg\` and run `src\gupkg.cmd install [PATH]`. It uses a system
  Python 3.11+ when one exists; otherwise it downloads a verified x64 CPython
  and pip into the copied folder's ignored `python\` directory on first use.
  The launcher prefers a package-local native command, then the adjacent
  `src\gupkg\gupkg.cmd` bootstrap, then a `gupkg.exe` on `PATH`.

Then open a new terminal and check it with `gupkg --version`. The [operations guide](docs/operations.md)
covers release build inputs, manager setup, the registry, and recovery commands,
and [Getting started](docs/getting_started.md) installs a first program.

## What gupkg manages

`gupkg` manages self-contained Windows applications that live on disk rather
than in a central package store. A package author puts an application's files,
its version number, and a small `pkg.toml` definition in one directory. From
that definition, `gupkg` selects the active version and makes the application
available to Windows: it can create Start Menu shortcuts, set environment
variables, add directories to PATH, and generate command-line wrappers. It can
also fetch application files when they are not already present and stage new
versions when updates are available.

The model is deliberately repair-friendly. Re-running an install re-applies
the declared Windows integration without re-downloading an application whose
files are already present.

## At a glance

- Works on Windows with Python 3.11 or newer.
- Treats the directory layout as the authority for package identity.
- Uses a canonical, strict `pkg.toml` format.
- Supports ZIP, Git, and package-local-script application sources.
- Checks and stages updates before activating a new immutable version.
- Supports GitHub Releases and trusted package-local Python update hooks.

Git is needed only by packages that declare a Git origin or Git updates; gupkg itself, including
the package registry, downloads plain ZIP files with Python's standard library. PowerShell is used to
create Windows shortcuts. Network access is required only when a configured
origin or update source is contacted.

## Architecture and organization

Each application has a **package root**. It contains one or more immutable
**version directories** and a `current` junction that points to the active
one. The version directory is the unit that `gupkg` installs and updates.

```text
<PackageName>/
  current/                         # NTFS junction to the active version
  v1.2.3/
    App/                            # application payload
    Icons/                          # optional icon assets
    Shortcuts/                      # optional package-owned assets
    pkg.local/                      # trusted update hook modules
    pkg.toml
```

`App` is the application payload: the files that actually run. For example,
`App` might contain `rg.exe`, a portable editor's executable and libraries, or
a Git checkout. `gupkg` never invents a particular application layout inside
`App`; it either starts with the files already there or populates them from the
configured origin. Shortcuts, PATH entries, environment values, and wrappers
normally point at files or directories beneath `App`.

`pkg.toml` sits beside `App` and describes the package version. It declares
how to obtain the payload when necessary and how to expose it to Windows. The
configuration uses package variables so that it does not need hard-coded
machine-specific paths: `$App`, `$VersionRoot`, `$Icons`, and
`$Shortcuts` resolve to matching directories in the installed version, while
`${version}` resolves to the version in the directory name. For example,

```toml
[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "$App"

[[bin]]
name = "rg"
target = "$App\\rg.exe"
```

This creates a shortcut to the executable, stores the full `App` path in an
environment variable, and creates a command wrapper that launches the same
executable. When a package needs a sibling directory, it should write that path
explicitly beneath `$VersionRoot`. The exact expansion rules are documented in
[Variables and expansion](#variables-and-expansion).

`Icons` and `Shortcuts` are optional package-owned asset directories. `Icons`
is a natural place for shortcut icons; `Shortcuts` is available to package
scripts or configuration that needs package-local shortcut assets. `pkg.local`
is reserved for trusted Python update-check and unpack hooks. `.gupkg`, created
at the package root by update operations, holds manager state, locks, receipts,
and disposable work files rather than application files.

Version directories must be named `v<upstream-version>` or
`v<upstream-version>.l<local-version>`. For example, `v1.2.3` has upstream
version `1.2.3` and local revision `0`, while `v1.2.3.l1` has local revision
`1`. New package definitions and update releases always use the plain version
name; gupkg never creates a `.lN` collision directory.
The package name is the package-root directory name. A name ending in
`-portable` is portable-only by convention.

You may give `gupkg` a version directory, a package root, or its `current`
junction. A package root without `current` is accepted only when it contains
exactly one version directory. Update checks and downloads accept any version
directory, so a historical definition can provide the update configuration.
Activation refuses a downloaded version when a newer installed version exists.

Installing a newer version advances `current`. Installing an older version
does not replace a newer active version unless `--allow-downgrade` is supplied. Old
versions are retained.

## Install a package

From a version directory:

```bat
gupkg install
```

Or pass a version directory or package root:

```bat
gupkg install C:\Packages\Ripgrep\v14.1.0.l1
gupkg install C:\Packages\Ripgrep
```

The default `auto` scope uses system scope for an administrator unless the
package is portable-only; otherwise it uses User scope. Select a scope
explicitly when needed:

```bat
gupkg --scope user install C:\Packages\Ripgrep
gupkg --scope system install C:\Packages\Ripgrep
```

System scope requires Administrator privileges. Portable-only packages cannot
be installed in system scope.

| Scope | Start Menu shortcut root | Registry environment and PATH | Wrapper directory |
| --- | --- | --- | --- |
| User | `%APPDATA%\Microsoft\Windows\Start Menu\opt` | `HKCU\Environment` | `%USERPROFILE%\bin` |
| Machine | `%PROGRAMDATA%\Microsoft\Windows\Start Menu\opt` | `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` | `<SYSTEMDRIVE>\bin` |

During an ordinary install, `gupkg`:

1. resolves the package and validates `pkg.toml`;
2. makes the selected version current when appropriate;
3. ensures `App` is a non-empty directory, populating it from `[origin]` when
   possible and otherwise failing the install;
4. creates declared shortcuts;
5. writes declared environment variables and PATH additions;
6. creates declared wrappers and ensures the scope's wrapper directory is on PATH.

An already populated `App` is left alone by default. Reinstalling still repairs
shortcuts, registry values, PATH entries, and wrappers. Use `--refresh-app` to
explicitly rebuild `App` from its origin before that repair.

## Commands and options

`gupkg` uses one explicit command grammar. `update` performs a full update by
default; `--check-only` and `--download-only` limit it without changing the
package. The root options are `--scope auto|user|system`, `--format
human|toml`, `--pause`, and `--allow-hook-dependency-install`.

### Central manager mode

The centrally installed executable and package-local mode are both supported.
Package-local launchers and explicit package paths keep existing behavior.
Manager mode is explicit: use the `manager` command with an optional
`--config PATH`. A discovered `gupkg-config.toml` never changes a package
invocation into manager mode.

The current manager schema is:

```toml
mode = "manager"
schema_version = 2

[packages]
system = 'C:\opt'
user = '%USERPROFILE%\opt'

[bin]
system = 'C:\bin'
user = '%USERPROFILE%\bin'

# Optional. These are the defaults; change them to move the cache or use a mirror.
[registry]
cache = '%LOCALAPPDATA%\gupkg\registry'
source = "https://github.com/guraltsev/pkg/archive/refs/tags/stable.zip"
```

Both package roots are required collection roots and must be distinct and
non-nesting. The bin roots must also be distinct. The `[registry]` table is
optional: `cache` (default `%LOCALAPPDATA%\gupkg\registry`) is where the
downloaded catalogue is kept and must not be inside either package root, and
`source` (default: the `stable` tag of the official repository) is the URL of a
ZIP archive containing a `pkgs` folder; it may be an `https://` mirror or a
`file:` URL on a share. The older `channel` key is accepted and ignored.
`gupkg manager init` writes this file for you. Relative paths resolve against the manager
file, `%NAME%` expands from the case-insensitive process environment, and a
leading `~` expands to the current user's home. Unknown variables and shell
substitutions are rejected. A missing root is reported as an incomplete scope
and blocks mutation; loading never creates roots. Scope comes from the
configured root, so a user target cannot silently become a machine
installation. Only schema version 2 is accepted; older files must be migrated
explicitly before manager commands can use them.

Manager workflows include `gupkg manager list`, `gupkg manager doctor`,
`gupkg manager update`, `gupkg manager init`, `gupkg manager registry sync|status`,
`gupkg manager search`, `gupkg manager install`, and
`gupkg manager self status|repair|update`. `list` is local-only. Doctor
validates configuration, roots, `current`, and manifests without contacting providers. A valid `current` is
authoritative: a lone version directory is not installed, and a broken or
escaping activation is broken. Bootstrap definitions remain available but are
never implicitly installed.

`gupkg --scope auto manager list` reports the manager inventory. Use
`gupkg manager tui` for the interactive manager interface. Manager update first shows a
non-installing plan with available, current, skipped, and failed-check counts.
The confirmation screen puts `Run planned updates` first and exposes scope,
checksum, and dependency auto-install settings. Execution remains scrollable
with per-target states and final totals, then refreshes inventory. Elevation
occurs before any mixed-scope mutation; declining it leaves packages unchanged.
Later safe targets continue after a failure.
To recover, fix the failed target and safely rerun the same check/confirm flow;
successful targets are revalidated and remain current. This migration changes
only orchestration: package content, version directories, `current`, and
`pkg.local` require no edits.

### Terminal interface

Run the simple interactive interface:

```bat
gupkg-tui.cmd C:\Packages\Example
```

You can also run `gupkg tui [PATH]` directly.

To open the manager interface, use `gupkg manager tui`. Manager selection is
always explicit; a package TUI invocation outside a package directory reports
an invalid package selection instead of switching modes.

The interface exposes install, update, and every configuration action with the
same package path, scope, and applicable flags as the command line. It
intentionally uses selections and plain output instead of a
frame-heavy terminal layout. On first use, `gupkg` automatically installs its
Textual dependency into `%LOCALAPPDATA%\gupkg\embedded\site-packages` when
using a system Python; the bundled runtime uses its own
`gupkg\python\Lib\site-packages` instead. The launcher Python is not modified.
`--pause` is omitted because the interface stays open after each operation.

```text
gupkg [global-options] <command> [command-options]
```

### Install

```bat
gupkg install C:\Packages\Tool
```

`install` activates the chosen version and applies its package definition. With
no path it installs the package in the current directory.

### Update

```bat
gupkg update C:\Packages\Tool
gupkg update C:\Packages\Tool --check-only
gupkg update C:\Packages\Tool --download-only
```

`update --check-only` is read-only and reports either an available release or
the current state. `update --download-only` checks again, downloads and verifies the release, and stages
it as a new version directory without changing `current`. A missing or empty
`App` remains repairable when upstream reports the same version if the plain
candidate version is not already present. A full `update` activates the staged
version and applies its shortcuts,
environment settings, PATH entries, and wrappers. There is no automatic update
policy or background update action. A full `update` after an earlier
`--download-only` activates the version that download staged instead of
downloading it again. A successful activation consumes its download receipt,
and the limiting modes never activate a staged version.

### Configuration

```bat
gupkg config-check C:\Packages\Ripgrep
gupkg config-fix C:\Packages\Ripgrep\v14.1.0.l1
gupkg config-fix --output C:\OldPackages\Ripgrep\pkg.toml C:\OldPackages\Ripgrep
```

`config-check` validates a package without changing it. `config-fix` creates a
starter file, synchronizes directory-owned metadata, or explicitly converts
recognized legacy files. It creates a timestamped backup before replacement by
default; `--no-backup` and `--backup=false` suppress it. Shortcut import is
controlled by `--import-shortcuts true|false`.

All options are accepted by the command parser; the following table notes where
they have an effect.

| Option | Meaning |
| --- | --- |
| `--scope auto\|user\|system` | Selects installation scope; defaults to `auto`. |
| `--format human\|toml` | Selects human output or one parseable TOML result document. |
| `--allow-downgrade` | For `install`, permits replacing `current` when it already targets a newer version. |
| `--refresh-app` | For `Install`, replaces `App` from `[origin]`, even when it is populated. |
| `--no-checksum` | Bypasses configured origin or update checksum verification and emits a warning. |
| `--allow-hook-dependency-install` | Allows trusted `pkg.local` update hooks to install missing imports for this invocation. |
| `--import-shortcuts true\|false` | For `config-fix`, imports `.lnk` files from `_shortcuts`; defaults to `true`. |
| `--output <path>` | Selects `config-fix` output for legacy conversion only. |
| `--no-backup`, `--backup=false` | Suppress the default timestamped `config-fix` backup. |
| `--check-only`, `--download-only` | Limit `update` or manager update work. |
| `--shim-linkage dynamic\|static` | For `install`, full `update`, manager update, and `manager self repair`: choose small wrappers with shared runtime DLLs (`dynamic`, default) or self-contained ones. |
| `--yes` | For `manager update`: skip the confirmation (required without a terminal or with `--format toml`). |
| `--force` | For `manager init`: replace an existing manager configuration. |
| `--offline` | For `manager search` and `manager install`: use only the cached registry. |
| `--config <file>`, `--max-depth N` | For `manager`: choose the configuration file; bound grouping-folder descent. |
| `--pause` | Waits for a keypress before exit. |
| `--version` | Prints the `gupkg` version and exits. |
| `--help` | Prints command help and exits. |

Exit status is `0` for success, `2` for a user/configuration error, `3` for a
mutation failure, and `4` for an unexpected internal error.

The convenience scripts call the same `gupkg.cli:main` entry point. The
console script and `python -m gupkg` therefore use identical parsing and
version information.

The internal `src\gupkg\gupkg.cmd` locates Python in this order:
`GUPKG_PYTHON`, `gupkg.python` beside the launcher, an existing
`python\python.exe`, then `python` from `PATH`, then the Windows `py -3`
launcher. When none is usable, it creates the ignored `python\` directory,
downloads CPython 3.12.10's official x64 embeddable package, verifies its
SHA-256 digest, and extracts it there. The Python bootstrap then writes runtime
support files and installs pip only inside that `python\` directory. The
downloaded runtime keeps manually installed packages in
`python\Lib\site-packages`; it does not alter a system Python. Use
`GUPKG_PYTHON` or `gupkg.python` to select another interpreter.

## `pkg.toml` reference

`pkg.toml` is optional for a simple pre-populated package. If it is absent,
`install` uses defaults and does not create a file. Use `config-fix` to create a
starter configuration, synchronize directory-owned metadata, or explicitly
convert legacy metadata. Existing files are backed up with a UTC timestamp
before a change unless `--no-backup` or `--backup=false` is supplied. When a
file exists, its schema is strict: unknown keys and legacy spellings are errors.

`name`, `version`, `localVersion`, and `only_portable` are package-owned
metadata. Their canonical values come from the directory name and layout.
`config-fix` synchronizes those fields while preserving unrelated runtime
configuration and comments where possible. An install stops on a metadata
mismatch. Run `gupkg config-fix <version-directory>` before installing to
synchronize it.

```toml
name = "Ripgrep"                 # package-root directory name
version = "14.1.0"               # version from v14.1.0.l1
localVersion = 1                 # local revision from .l1
only_portable = false            # must agree with the -portable convention
description = "Fast text search" # descriptive metadata only
homepage = "https://example.com" # descriptive metadata only

[origin]
url = "https://example.invalid/ripgrep.zip"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
extractSubdir = "ripgrep-14.1.0"

[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "$App"

[[path]]
value = "$App"

[[bin]]
name = "rg"
target = "$App\\rg.exe"
arguments = ["--color=auto"]
```

Top-level keys are exactly: `name`, `version`, `localVersion`, `description`,
`homepage`, `only_portable`, `origin`, `update`, `shortcut`, `environment`,
`path`, and `bin`.

### Application origins

`[origin]` is optional only when the version already contains a non-empty
`App`. It supplies `App` when that directory is missing or empty,
unless `--refresh-app` is used. An origin is one of the following.

**ZIP origin.** Omit `mode` and supply an HTTP(S) archive URL. `checksum`, when
present, must be `sha256:` followed by 64 hexadecimal characters. `extractSubdir`
selects a directory inside the archive; its contents become `App`.

```toml
[origin]
url = "https://example.invalid/tool-portable.zip"
checksum = "sha256:<64-hex-digits>"
extractSubdir = "tool-portable"
```

**Git origin.** Set `mode = "git"`, provide a safe Git URL, and optionally a
full `refs/...` ref. The ref defaults to `refs/heads/main`. `gupkg` resolves the
ref and checks out that exact commit into `App`.

```toml
[origin]
mode = "git"
url = "git@github.com:owner/repository.git"
ref = "refs/heads/main"
```

**Script origin.** Supply a package-local `.ps1`, `.cmd`, `.bat`, or `.exe`
path. It must stay beneath the version directory. `gupkg` runs it with its
directory as the working directory, sends a JSON document on standard input,
and requires a successful exit status and a non-empty `App`. The JSON contains
`config`, `identity`, and `PkgVars` (`PkgRoot`, `App`, `Icons`, and
`Shortcuts`). Script origins clear `App` first only when `--refresh-app` is
used.

```toml
[origin]
script = "scripts\\populate-app.ps1"
```

**Module origin.** Supply a package-local Python module beneath `pkg.local`.
It must declare `PKG_MODULE_API = 1` and define `populate_app(context)`. The
module receives the same `config`, `identity`, and `PkgVars` mappings as a
script origin, plus `apiVersion = 1`.

```toml
[origin]
module = "pkg.local/populate_app.py"
```

`url`, `script`, and `module` are mutually exclusive. Set `mode = "module"`
only when declaring `module`; ZIP, script, and module modes are otherwise
inferred. Git origins cannot use `checksum` or
`extractSubdir`.

Use repeated `[[origin.versions]]` tables to record historical sources. Every
entry needs a unique `version`; it may initially contain only that version.
If `[origin]` has no inline `url` or `script`, the entry matching the package's
top-level `version` becomes the active source.

```toml
[origin]

[[origin.versions]]
version = "1.0.0"
url = "https://example.invalid/tool-1.0.0.zip"
checksum = "sha256:<64-hex-digits>"
```

### Integration tables

Each `[[shortcut]]` table accepts `name`, `targetPath`, `arguments`,
`workingDirectory`, `iconLocation`, and `description`. Only `name` and
`targetPath` are required. A `.lnk` suffix is added to `name` when absent.

Each `[[environment]]` table requires exact-case `Name` and `Value` keys.
Values are written as expandable Windows registry strings. Each `[[path]]`
table has one `value`; normalized entries are appended only when they are not
already present (case-insensitive, ignoring a trailing slash).

Each `[[bin]]` table normally declares a native executable shim with `name`
and `target`:

```toml
[[bin]]
name = "tool"
target = "$App\\tool.exe"
type = "console"            # default; use "gui" for desktop applications
arguments = ["--safe"]      # fixed arguments precede caller arguments
forward_args = true          # default
elevate = false              # default
working_dir = "$App"        # optional
```

The installed command is `<name>.exe` (an explicit `.exe` suffix is also
accepted) with a matching `<name>.config.toml`. The launcher invokes `target`
directly, without a shell. Use `type = "gui"` when the command should not
create or attach to a console.

When shell behavior or custom file content is necessary, `content` remains an
explicit escape hatch. It cannot be combined with shim options:

```toml
[[bin]]
name = "tool.cmd"
content = "@echo off\r\ncall \"$App\\tool.cmd\" %*\r\n"
```

Shortcut and wrapper names are expanded before placement. A simple name goes
under the scope's default root; nested relative paths are allowed. Absolute
paths and `..` traversal are also allowed, but an output outside the default
root produces a warning. This is intentional flexibility, so review such
configuration carefully.

### Variables and expansion

`$App`, `$VersionRoot`, `$Icons`, and `$Shortcuts` expand to the corresponding
directories in the selected version. `$VersionRoot` is the
version directory itself. `${version}` expands to the upstream version. Braced
environment references such as `${USERPROFILE}` expand from the process
environment and must resolve. `$$` becomes a literal dollar sign.

In normal configuration fields, an unbraced non-package token such as `$NAME`
is an error. In `[[bin]].content`, such tokens remain literal so native batch,
PowerShell, and shell variables work as expected.

## Updates

Updates follow **check → download → install**. `gupkg update --check-only` only
discovers a candidate. `gupkg update --download-only` stages a complete new version under
`<package-root>\.gupkg\work`, commits it as a new `v<version>` directory,
and records a receipt. `gupkg update` activates the most recently
downloaded version through the regular install workflow.
`gupkg update` performs those three steps as one explicit command.
Update state, locks, receipts, and disposable work files all live in
`<package-root>\.gupkg`; package repositories should ignore `/.gupkg/`.

Updates are never started or activated automatically. A package administrator
or a scheduler chooses when to run each explicit update command.

Every update needs `[update.payload]` and normally `[update.check]`:

```toml
[update.check]
mode = "github"
assetName = "tool-${version}-windows-x86_64.zip"
# tagPrefix = "release/" # remove a publisher-specific namespace before parsing

[update.payload]
mode = "zip"
extractSubdir = "tool"
```

#### Update checks

`[update.check].mode` is one of:

- `github`: requires `[origin].url` to be an `https://github.com/owner/repository`
  page URL and requires `assetName`. The built-in checker reads GitHub's latest
  release, removes a conventional leading `v` from its tag, and requires
  exactly one uploaded asset with that name. `${version}` in `assetName`
  expands to the discovered release version. GitHub's SHA-256 asset digest is
  used when available. Set `tagPrefix` when the publisher namespaces tags (for
  example, `release/v1.2.3` with `tagPrefix = "release/"`).
- `git`: checks `appPath` (default `App`) against `remote` (default `origin`)
  and a full `ref` (default `refs/heads/main`). For a Git origin, `check` may
  be omitted and defaults to that origin's ref; an explicit ref must match it.
- `module`: imports a trusted `.py` module beneath `pkg.local` (default
  `pkg.local/check_update.py`) and calls `check_update(context)`. `channel`
  defaults to `stable`.

Module checks must declare `PKG_MODULE_API = 1`. Their context has
`apiVersion`, `current` identity fields (including `appReady`),
package/version/App paths, persisted state, and `channel`. Return `None` only
when the upstream version is current and its payload is healthy. When
`appReady` is false, return the current candidate again so `update --download-only`
can stage a repair. Candidate mappings contain non-empty `candidateId`,
`version`, and `url`. Optional candidate fields are
`sha256` (64 hex digits), `fileName`, `headers`, and `extractSubdir`. Candidate
versions must be safe version-directory values and may not go backward.

`gupkg` installs every dependency declared by its own optional runtime features
into `%LOCALAPPDATA%\gupkg\embedded\site-packages` when using a system Python;
the bundled runtime instead uses its own `python\Lib\site-packages` directory.
The launcher Python is not modified. Trusted package-local hooks never trigger
dependency installation by default. When a hook needs an unavailable import,
`gupkg` reports it and stops. Pass `--allow-hook-dependency-install` to explicitly
allow installation and retrying for that command. Installations use the active
interpreter's `pip`. Trusted package-local hooks are not sandboxed.

#### Update payloads

`[update.payload].mode` is one of:

- `zip`: downloads a verified ZIP payload and populates a new `App`. A direct
  candidate `.exe` is instead copied intact into `App`; installers need a
  module payload.
- `module`: downloads the candidate artifact and calls trusted
  `pkg.local/unpack_app.py` by default. It must declare `PKG_MODULE_API = 1`
  and define `unpack_app(context)`. The context contains `candidate` and paths
  for `artifact`, `stageRoot`, and `stageApp`; the hook must leave `stageApp`
  non-empty.
- `git`: clones the checked Git candidate into a new immutable version.

`git` requires a Git check. Downloaded candidates require a SHA-256 checksum
unless `ignore_checksum = true` in the payload or
`--no-checksum` is used.

ZIP payloads may use `extractSubdir`, a candidate-provided `extractSubdir`, or
explicit extraction mappings. `[[update.payload.extract]]` uses a ZIP-root
shell-wildcard `src` and an `$App`-relative `dest`. A `src` ending in `/` copies
the matched directory's contents; otherwise a matched directory is copied as a
directory. `extract` cannot be combined with `extractSubdir`.

```toml
[update.payload]
mode = "zip"

[[update.payload.extract]]
src = "pandoc-*/"
dest = ""

[[update.payload.rename]]
src = "pandoc-${version}.exe"
dest = "pandoc.exe"
```

`[[update.payload.rename]]` renames exact, safe paths inside staged `App`
after extraction; it cannot overwrite an existing destination. `maxSizeMB` is
accepted by the current schema for future policy use but is not currently
enforced.

#### Install steps

The built-in update sequence is a release check followed by the configured
payload step. Existing packages need no step declaration. To run package-local
work after that payload has been unpacked, declare the ordered sequence
explicitly. The first entry must be the built-in `payload` step; each later
entry is a trusted Python module beneath `pkg.local`.

```toml
[[update.steps]]
mode = "payload"

[[update.steps]]
mode = "module"
module = "pkg.local/post_install.py"
```

Install-step modules declare `PKG_MODULE_API = 1` and define
`install_step(context)`. They receive the candidate and `paths` for the staged
`stageRoot`, `stageApp`, and downloaded `artifact` (or `None` for Git
payloads). Steps run in declaration order before the new version is committed,
so a failure leaves the installed version unchanged.

```python
PKG_MODULE_API = 1

def install_step(context):
    context["paths"]["stageApp"].joinpath("unwanted-file.txt").unlink(
        missing_ok=True
    )
```

Versions beginning with `bootstrap` are templates rather than active payloads.
Installing one with a Git or module update configuration downloads and
activates the first immutable version, leaving the template itself without an `App`.
A Git bootstrap commonly uses `vbootstrap-git` with `payload.mode = "git"`.

## Configuration, validation, and migration

Use `config-check` before deploying a package definition. It validates canonical
TOML, directory-derived metadata, historical-origin consistency, package-local
origin scripts, and configured update modules without modifying the package.

Use `config-fix` to create a starter `pkg.toml` or repair metadata while
keeping runtime settings. It imports `_shortcuts` `.lnk` files by default and
archives each imported source as `.lnk.imported`; pass
`--import-shortcuts false` to skip that import. It does not populate `App` or
install components.

`config-fix` is the explicit migration path for older JSON-based layouts. It
uses the existing converter, validates the complete replacement before writing,
and creates a timestamped sibling backup unless backup is disabled. Use
`--output PATH` only for this conversion mode.

The standalone helpers remain available from `src`:

```bat
python gupkg\legacy_to_gupkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1
python gupkg\shortcuts_to_gupkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1
```

The shortcut importer reads `.lnk` files from `_shortcuts`, converts
package-owned paths back to package variables, and updates matching
`[[shortcut]]` tables. After a successful write, it archives each imported
source as `.lnk.imported`. Both helpers are migration tools, not the supported
runtime package API.

## Development

Implementation and test guidance is in [docs/development_guide.md](docs/development_guide.md).
The runtime module overview and migration-helper details are in
[src/gupkg/README.md](src/gupkg/README.md).
