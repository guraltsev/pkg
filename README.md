# pkg

`pkg` is a single-file Windows package tool for locally cached applications.

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

You can run `pkg` from a version directory, a `current` junction, or the package root.

## Actions

- `Install` updates the `current` junction when appropriate, then applies shortcuts, environment variables, PATH entries, and wrapper files.
- `UpdateConfig` creates a starter `pkg.toml` when missing, or syncs only filesystem-derived metadata in an existing `pkg.toml`.

## Exit codes

- `0` success, including “no changes needed”
- `2` user/config/input/dependency problem
- `3` system mutation failure
- `4` unexpected internal failure

## Config notes

`Install` does not auto-create `pkg.toml` when it is missing.

For existing `pkg.toml`, `UpdateConfig` preserves comments, unknown keys, and existing layout while syncing only these owned metadata keys:

- `name`
- `version`
- `localVersion`
- `only_portable`

`${VAR}` is the recommended environment-expansion syntax. Plain `$VAR` remains supported in general config fields for compatibility, but wrapper script content does not treat plain `$VAR` as pkg expansion unless it is a package variable such as `$App`.

## Minimal config example

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

## Dependency note

`tomlkit` is preferred for round-trip TOML editing. When it is unavailable, `pkg` falls back to a narrow metadata-only updater for `UpdateConfig` so existing files can still be synced without broad rewrites.
