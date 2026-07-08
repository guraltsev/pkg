# pkg

`pkg` is a Windows package tool for locally cached applications in versioned
directories. A package can already include `App/`, or it can declare an
`[origin]` step that populates `App/` during install.

It does four main things:

- keeps the package-level `current` junction pointed at the active version
- populates a missing or empty `App/` from `[origin]` when configured
- creates shortcuts from `pkg.toml`
- writes environment variables and PATH entries for the selected scope
- creates wrapper files in the selected scope bin directory when `[[bin]]`
  entries are configured

`pkg` is repair-oriented. Re-running install for the same version is expected:
it will reapply shortcuts, environment variables, PATH entries, and wrapper
files so a broken install can be restored without downloading anything again.

## Requirements

- Windows
- Python 3.11+

## Package layout

```text
<PackageName>/
  current/                # NTFS junction to the active version
  v1.2.3.l1/
    App/
    Icons/
    Shortcuts/
    pkg.toml
```

You can point `pkg` at any of these:

- a version directory
- the package root
- the `current` junction

Version directories use this naming scheme:

```text
v<upstream-version>.l<local-version>
```

## Commands

Install in User scope from the current directory:

```bat
python pkg.py
```

Install a specific package:

```bat
python pkg.py C:\Packages\Ripgrep\v14.1.0.l1
```

Install in Machine scope:

```bat
python pkg.py --scope Machine C:\Packages\Ripgrep\v14.1.0.l1
```

Synchronize or create `pkg.toml` metadata only:

```bat
python pkg.py --action UpdateConfig C:\Packages\Ripgrep\v14.1.0.l1
```

Repair mismatched top-level metadata during install:

```bat
python pkg.py --fix-config C:\Packages\Ripgrep\v14.1.0.l1
```

Refresh `App/` from the package origin before reinstalling components:

```bat
python pkg.py --refresh-app C:\Packages\Ripgrep\v14.1.0.l1
```

The convenience wrappers call the same entry point:

- `install.cmd`
- `install-machine.cmd`
- `update-config.cmd`
- `pkg.cmd`

Run `python pkg.py --help` for the full CLI reference.

Validate package metadata without installing:

```bat
python pkg.py --action HealthCheck C:\Packages\Ripgrep\v14.1.0.l1
```

## Bootstrap interpreter selection

`pkg.cmd` chooses Python in this order:

1. `--python <exe-or-command>`
2. `PKG_PYTHON`
3. `pkg.python` next to `pkg.cmd`
4. `python` from `PATH`

## `pkg.toml`

`pkg` accepts one canonical schema.

Top-level metadata owned by `pkg`:

- `name`
- `version`
- `localVersion`
- `only_portable`

Runtime tables:

- `[origin]`
- `[[shortcut]]`
- `[[environment]]`
- `[[path]]`
- `[[bin]]`

Minimal example:

```toml
name = "Ripgrep"
version = "14.1.0"
localVersion = 1
only_portable = false

[origin]
url = "https://example.invalid/ripgrep.zip"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
extractSubdir = "ripgrep"

[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "${USERPROFILE}\\Ripgrep"

[[path]]
value = "$App"

[[bin]]
name = "rg.cmd"
content = "@echo off\r\n\"$App\\rg.exe\" %*\r\n"
```

`[origin]` is optional. When present, install populates `App/` only if the
directory is missing or empty. If `App/` already contains anything, install
skips origin population and still repairs shortcuts, environment variables,
PATH entries, and wrappers.

Built-in origin mode downloads an HTTP(S) zip archive:

```toml
[origin]
url = "https://example.invalid/tool-portable.zip"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
extractSubdir = "tool-portable"
```

`checksum` is optional, but when present it is verified by default. Use
`--no-checksum` only for an explicit local override; install prints a warning
and skips verification. `extractSubdir` selects a directory inside the archive
whose contents become `App/`.

Historical origins can be recorded with repeated `[[origin.versions]]` tables.
Entries may contain only `version` until you try to install from that entry:

```toml
[origin]
url = "https://example.invalid/tool-2.0.0.zip"

[[origin.versions]]
version = "1.0.0"
url = "https://example.invalid/tool-1.0.0.zip"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
```

When the current origin is one of the versioned entries, omit top-level `url`
or `script`. `pkg` selects the entry whose `version` matches the top-level
package `version`:

```toml
version = "1.0.0"

[origin]

[[origin.versions]]
version = "1.0.0"
url = "https://example.invalid/tool-1.0.0.zip"
```

`HealthCheck` verifies that origin history is internally consistent: versioned
entries must have unique `version` values, history-only origins must include
an entry matching the top-level package `version`, and script origins must
point to supported package-local script files when a script is declared.

Script origin mode runs a package-local script:

```toml
[origin]
script = "scripts\\populate-app.ps1"
```

Scripts must live under the version directory and use `.ps1`, `.cmd`, `.bat`,
or `.exe`. The script receives JSON on stdin, including `PkgVars.App`,
`PkgVars.Icons`, and `PkgVars.Shortcuts`, and must leave `App/` non-empty.

`--refresh-app` explicitly repopulates an already populated `App/`. Zip origin
prepares the new payload before replacing the directory. Script origin clears
`App/` first, then runs the script.

## Variable rules

Package variables:

- `$App`
- `$Icons`
- `$Shortcuts`

Environment-variable syntax:

- `${VAR}` expands from the process environment
- `$$` becomes a literal `$`

In normal config fields, unresolved `${VAR}` values are errors. Inside
`[[bin]]` content, plain non-package `$NAME` text is left alone so batch,
PowerShell, and shell variables keep their native meaning.

## Output placement

`shortcut.name` and `bin.name` are expanded before placement. Each may be a
simple name, a nested relative path under the default shortcut/bin root, or a
path-like destination outside that root.

Absolute paths and parent traversal are allowed. When the final destination is
outside the default root, install prints a warning but still creates the output.

## `UpdateConfig` behavior

`UpdateConfig` is intentionally narrow:

- if `pkg.toml` is missing, it creates a starter file with synchronized
  metadata and commented examples
- if `pkg.toml` already exists and uses the canonical schema, it synchronizes
  only the owned top-level metadata keys
- if you point it at a package root that does not yet have `current`, it uses
  the only version directory under that root when there is exactly one
- it preserves unrelated runtime content and comments when possible

`Install` does not create `pkg.toml` when it is missing. It uses defaults and
continues.

## Helper scripts

Best-effort migration helpers live in [`pkg.modules/`](src/pkg.modules/README.md).
They are for manual transitions from older formats and are not part of the
supported runtime surface.

## Development notes

Contributor notes live in [`docs/development.md`](docs/development.md).
