# pkg

`pkg` is a Windows package tool for locally cached applications that already
live on disk in versioned directories.

It does four main things:

- keeps the package-level `current` junction pointed at the active version
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

The convenience wrappers call the same entry point:

- `install.cmd`
- `install-machine.cmd`
- `update-config.cmd`
- `pkg.cmd`

Run `python pkg.py --help` for the full CLI reference.

## `pkg.toml`

`pkg` accepts one canonical schema.

Top-level metadata owned by `pkg`:

- `name`
- `version`
- `localVersion`
- `only_portable`

Runtime tables:

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

## `UpdateConfig` behavior

`UpdateConfig` is intentionally narrow:

- if `pkg.toml` is missing, it creates a starter file with synchronized
  metadata and commented examples
- if `pkg.toml` already exists and uses the canonical schema, it synchronizes
  only the owned top-level metadata keys
- it preserves unrelated runtime content and comments when possible

`Install` does not create `pkg.toml` when it is missing. It uses defaults and
continues.

## Helper scripts

Best-effort migration helpers live in [`helper_scripts/`](helper_scripts/README.md).
They are for manual transitions from older formats and are not part of the
supported runtime surface.

## Development notes

Contributor notes live in [`docs/development.md`](docs/development.md).
