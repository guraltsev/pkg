# pkg

`pkg` manages self-contained Windows applications that live on disk rather
than in a central package store. A package author puts an application's files,
its version number, and a small `pkg.toml` definition in one directory. From
that definition, `pkg` selects the active version and makes the application
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

Git must be available for Git origins or Git updates. PowerShell is used to
create Windows shortcuts. Network access is required only when a configured
origin or update source is contacted.

## Architecture and organization

Each application has a **package root**. It contains one or more immutable
**version directories** and a `current` junction that points to the active
one. The version directory is the unit that `pkg` installs and updates.

```text
<PackageName>/
  current/                         # NTFS junction to the active version
  v1.2.3.l1/
    App/                            # application payload
    Icons/                          # optional icon assets
    Shortcuts/                      # optional package-owned assets
    pkg.local/                      # trusted update hook modules
    pkg.toml
```

`App` is the application payload: the files that actually run. For example,
`App` might contain `rg.exe`, a portable editor's executable and libraries, or
a Git checkout. `pkg` never invents a particular application layout inside
`App`; it either starts with the files already there or populates them from the
configured origin. Shortcuts, PATH entries, environment values, and wrappers
normally point at files or directories beneath `App`.

`pkg.toml` sits beside `App` and describes the package version. It declares
how to obtain `App` when necessary and how to expose it to Windows. The
configuration uses package variables so that it does not need hard-coded
machine-specific paths: `$App`, `$Icons`, and `$Shortcuts` resolve to the
matching directories in the installed version, while `${version}` resolves to
the version in the directory name. For example,

```toml
[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "$App"

[[bin]]
name = "rg.cmd"
command = "\"$App\\rg.exe\""
forward_args = true
```

This creates a shortcut to the executable, stores the full `App` path in an
environment variable, and creates a command wrapper that launches the same
executable. The exact expansion rules are documented in [Variables and
expansion](#variables-and-expansion).

`Icons` and `Shortcuts` are optional package-owned asset directories. `Icons`
is a natural place for shortcut icons; `Shortcuts` is available to package
scripts or configuration that needs package-local shortcut assets. `pkg.local`
is reserved for trusted Python update-check and unpack hooks. `.pkg`, created
at the package root by update operations, holds manager state, locks, receipts,
and disposable work files rather than application files.

Version directories must be named `v<upstream-version>.l<local-version>`.
For example, `v1.2.3.l1` has upstream version `1.2.3` and local revision `1`.
The package name is the package-root directory name. A name ending in
`-portable` is portable-only by convention.

You may give `pkg` a version directory, a package root, or its `current`
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
pkg.cmd
```

Or pass a version directory or package root:

```bat
pkg.cmd C:\Packages\Ripgrep\v14.1.0.l1
pkg.cmd C:\Packages\Ripgrep
```

The default `Auto` scope uses Machine scope for an administrator unless the
package is portable-only; otherwise it uses User scope. Select a scope
explicitly when needed:

```bat
pkg.cmd --scope User C:\Packages\Ripgrep
pkg.cmd --scope Machine C:\Packages\Ripgrep
```

Machine scope requires Administrator privileges. Portable-only packages cannot
be installed in Machine scope.

| Scope | Start Menu shortcut root | Registry environment and PATH | Wrapper directory |
| --- | --- | --- | --- |
| User | `%APPDATA%\Microsoft\Windows\Start Menu\opt` | `HKCU\Environment` | `%USERPROFILE%\bin` |
| Machine | `%PROGRAMDATA%\Microsoft\Windows\Start Menu\opt` | `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` | `<SYSTEMDRIVE>\bin` |

During an ordinary install, `pkg`:

1. resolves the package and validates `pkg.toml`;
2. makes the selected version current when appropriate;
3. populates `App` if an `[origin]` is configured and `App` is missing or empty;
4. creates declared shortcuts;
5. writes declared environment variables and PATH additions;
6. creates declared wrappers and ensures the scope's wrapper directory is on PATH.

An already populated `App` is left alone by default. Reinstalling still repairs
shortcuts, registry values, PATH entries, and wrappers. Use `--refresh-app` to
explicitly rebuild `App` from its origin before that repair.

## Commands and options

`pkg` uses a small verb-based command line. Every update is explicit: check
what is available, download it into a new version directory, then choose when
to make that downloaded version current.

### Terminal interface

Run the simple interactive interface:

```bat
tui.cmd
```

You can also run `pkg.cmd tui` directly.

The interface exposes install, each update stage, and every configuration
action with the same package path, scope, and applicable flags as the command
line. It intentionally uses selections and plain output instead of a
frame-heavy terminal layout. On first use, `pkg` automatically installs its
Textual dependency into `%LOCALAPPDATA%\pkg\dependencies`; the launcher Python
is not modified. `--pause` is omitted because the interface stays open after
each operation.

```text
pkg.cmd [options] <command> [subcommand] [path]
```

### Install

```bat
pkg.cmd install C:\Packages\Tool
```

`install` activates the chosen version and applies its package definition. With
no path it installs the package in the current directory.

### Upgrade

```bat
pkg.cmd upgrade check C:\Packages\Tool
pkg.cmd upgrade download C:\Packages\Tool
pkg.cmd upgrade install C:\Packages\Tool
pkg.cmd upgrade full C:\Packages\Tool
```

`upgrade check` is read-only and reports either `Available: ...` or `Current: ...`.
When it reports an available release, it also tells you to run `upgrade download`;
the check summary explicitly says that no files were changed.
`upgrade download` checks again, downloads and verifies the release, and stages
it as a new version directory without changing `current`. `upgrade install`
activates the most recently downloaded version and applies its shortcuts,
environment settings, PATH entries, and wrappers. There is no automatic update
policy or background update action. A successful activation consumes its
download receipt, so `upgrade install` cannot silently reinstall an old staged
version; run `upgrade download` before each activation. `upgrade full` checks,
downloads, and activates an update from the current package directory or package
root.

### Configuration

```bat
pkg.cmd config check C:\Packages\Ripgrep
pkg.cmd config update C:\Packages\Ripgrep\v14.1.0.l1
pkg.cmd --dry-run config from-legacy C:\OldPackages\Ripgrep
```

`config check` validates a package without installing it. `config update`
creates a starter `pkg.toml` when absent or synchronizes its directory-owned
metadata. `config from-legacy` is a one-time, best-effort migration tool for
older package formats.

All options are accepted by the command parser; the following table notes where
they have an effect.

| Option | Meaning |
| --- | --- |
| `--scope Auto\|User\|Machine` | Selects installation scope; defaults to `Auto`. Relevant to install and activation actions. |
| `--use-defaults` | For `Install`, continues with defaults when an existing `pkg.toml` cannot be parsed or validated. |
| `--allow-downgrade` | For `install`, permits replacing `current` when it already targets a newer version. |
| `--refresh-app` | For `Install`, replaces `App` from `[origin]`, even when it is populated. |
| `--no-checksum` | Bypasses configured origin or update checksum verification and emits a warning. |
| `--local-deps-autoinstall` | Allows trusted `pkg.local` update hooks to install missing imports. Hooks otherwise report unavailable dependencies without installing anything. |
| `--output <path>` | Selects `config from-legacy` output; the default is `<path>\pkg.toml`. |
| `--dry-run` | For `config from-legacy`, writes generated TOML to standard output without changing files. |
| `--toml` | Adds `ok`, `changed`, and `status` fields to normal output. |
| `--pause` | Waits for a keypress before exit. |
| `--version` | Prints the `pkg` version and exits. |
| `--help`, `--help-extended` | Prints standard or expanded CLI help and exits. |

Exit status is `0` for success, `2` for a user/configuration error, `3` for a
mutation failure, and `4` for an unexpected internal error.

The convenience scripts call the same entry point and add `--pause` where
appropriate. They use the same `install`, `upgrade`, and `config` commands
documented above.

`pkg.cmd` locates Python in this order: `PKG_PYTHON`, `pkg.python` beside the
launcher, then `python` from `PATH`.

## `pkg.toml` reference

`pkg.toml` is optional for a simple pre-populated package. If it is absent,
`install` uses defaults and does not create a file; `config update` creates a
starter configuration. When a file exists, its schema is strict: unknown keys
and legacy spellings are errors.

`name`, `version`, `localVersion`, and `only_portable` are package-owned
metadata. Their canonical values come from the directory name and layout.
`config update` synchronizes those fields while preserving unrelated runtime
configuration and comments where possible. An install stops on a metadata
mismatch. Run `pkg config update <version-directory>` before installing to
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
name = "rg.cmd"
command = "\"$App\\rg.exe\""
forward_args = true
extra_args = "--color=auto"
```

Top-level keys are exactly: `name`, `version`, `localVersion`, `description`,
`homepage`, `only_portable`, `origin`, `update`, `shortcut`, `environment`,
`path`, and `bin`.

### Application origins

`[origin]` is optional. It supplies `App` only when `App` is missing or empty,
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
full `refs/...` ref. The ref defaults to `refs/heads/main`. `pkg` resolves the
ref and checks out that exact commit into `App`.

```toml
[origin]
mode = "git"
url = "git@github.com:owner/repository.git"
ref = "refs/heads/main"
```

**Script origin.** Supply a package-local `.ps1`, `.cmd`, `.bat`, or `.exe`
path. It must stay beneath the version directory. `pkg` runs it with its
directory as the working directory, sends a JSON document on standard input,
and requires a successful exit status and a non-empty `App`. The JSON contains
`config`, `identity`, and `PkgVars` (`PkgRoot`, `App`, `Icons`, and
`Shortcuts`). Script origins clear `App` first only when `--refresh-app` is
used.

```toml
[origin]
script = "scripts\\populate-app.ps1"
```

`url` and `script` are mutually exclusive. `mode` is valid only as `git`; ZIP
and script modes are inferred. Git origins cannot use `checksum` or
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

Each `[[bin]]` table requires `name` and either `content` or `command`:

```toml
# Write exactly this wrapper content.
[[bin]]
name = "tool.cmd"
content = "@echo off\r\ncall \"$App\\tool.exe\" %*\r\n"

# Or let pkg produce @echo off / call <command>.
[[bin]]
name = "tool.cmd"
command = "\"$App\\tool.exe\""
extra_args = "--safe"
forward_args = true
```

`extra_args` and `forward_args` are valid only with `command`; `forward_args`
defaults to `false`. With command form, `pkg` emits `@echo off`, then `call
<command>`, followed by `extra_args` and `%*` when requested.

Shortcut and wrapper names are expanded before placement. A simple name goes
under the scope's default root; nested relative paths are allowed. Absolute
paths and `..` traversal are also allowed, but an output outside the default
root produces a warning. This is intentional flexibility, so review such
configuration carefully.

### Variables and expansion

`$App`, `$Icons`, and `$Shortcuts` expand to the corresponding directories in
the selected version. `${version}` expands to the upstream version. Braced
environment references such as `${USERPROFILE}` expand from the process
environment and must resolve. `$$` becomes a literal dollar sign.

In normal configuration fields, an unbraced non-package token such as `$NAME`
is an error. In `[[bin]].content` and generated command wrappers, such tokens
remain literal so native batch, PowerShell, and shell variables work as
expected.

## Updates

Updates follow **check → download → install**. `pkg upgrade check` only
discovers a candidate. `pkg upgrade download` stages a complete new version under
`<package-root>\.pkg\work`, commits it as a new `v<version>.lN` directory,
and records a receipt. `pkg upgrade install` activates the most recently
downloaded version through the regular install workflow.
`pkg upgrade full` performs those three steps as one explicit command.
Update state, locks, receipts, and disposable work files all live in
`<package-root>\.pkg`; package repositories should ignore `/.pkg/`.

Updates are never started or activated automatically. A package administrator
or a scheduler chooses when to run each explicit upgrade command.

Every update needs `[update.payload]` and normally `[update.check]`:

```toml
[update.check]
mode = "github"
assetName = "tool-${version}-windows-x86_64.zip"

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
  used when available.
- `git`: checks `appPath` (default `App`) against `remote` (default `origin`)
  and a full `ref` (default `refs/heads/main`). For a Git origin, `check` may
  be omitted and defaults to that origin's ref; an explicit ref must match it.
- `module`: imports a trusted `.py` module beneath `pkg.local` (default
  `pkg.local/check_update.py`) and calls `check_update(context)`. `channel`
  defaults to `stable`.

Module checks must declare `PKG_MODULE_API = 1`. Their context has
`apiVersion`, `current` identity fields, package/version/App paths, persisted
state, and `channel`. Return `None` when current, or a mapping with
non-empty `candidateId`, `version`, and `url`. Optional candidate fields are
`sha256` (64 hex digits), `fileName`, `headers`, and `extractSubdir`. Candidate
versions must be safe version-directory values and may not go backward.

`pkg` installs every dependency declared by its own optional runtime features
into `%LOCALAPPDATA%\pkg\dependencies`; the launcher Python is not modified.
Trusted package-local hooks never trigger dependency installation by default.
When a hook needs an unavailable import, `pkg` reports it and stops. Pass
`--local-deps-autoinstall` to explicitly allow installation and retrying for
that command. Installations prefer `uv` and otherwise use that environment's
`pip`. Trusted package-local hooks are not sandboxed.

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

Versions beginning with `bootstrap` are templates rather than active payloads.
Installing one with a Git or module update configuration downloads and
activates the first immutable version, leaving the template itself without an `App`.
A Git bootstrap commonly uses `vbootstrap-git.l1` with `payload.mode = "git"`.

## Configuration, validation, and migration

Use `config check` before deploying a package definition. It validates canonical
TOML, directory-derived metadata, historical-origin consistency, package-local
origin scripts, and configured update modules without modifying the package.

Use `config update` to create a starter `pkg.toml` or repair metadata while
keeping runtime settings. It does not populate `App` or install components.

`config from-legacy` is a best-effort migration aid for older JSON-based layouts.
It recognizes common `opt_pkg.json`, `environment*.json`/`env*.json`,
`shortcut*.json`, and `bin*.json` inputs; unknown and malformed material may
become warnings. Review its output before installing. Existing output is
backed up as `pkg.toml.bak`, then incremented suffixes when necessary.

The standalone helpers remain available from `src`:

```bat
python pkg\legacy_to_pkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1
python pkg\shortcuts_to_pkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1
```

The shortcut importer reads `.lnk` files from `_shortcuts`, converts
package-owned paths back to package variables, and updates matching
`[[shortcut]]` tables. Both helpers are migration tools, not the supported
runtime package API.

## Development

Implementation and test guidance is in [docs/development_guide.md](docs/development_guide.md).
The runtime module overview and migration-helper details are in
[src/pkg/README.md](src/pkg/README.md).
