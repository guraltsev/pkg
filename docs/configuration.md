# Configuration reference

## Supported package layout

```text
<PackageName>/
  current/
  v1.2.3.l1/
    App/
    Icons/
    Shortcuts/
    pkg.toml
```

Version directories must use the `v<upstream>.l<local>` naming convention.

## Canonical schema only

`pkg` accepts one canonical `pkg.toml` schema:

- top-level package metadata lives at the top level
- runtime data lives in `[[shortcut]]`, `[[environment]]`, `[[path]]`, and `[[bin]]`
- legacy aliases and `[[main]]` are rejected

Keys are validated exactly. The accepted spellings are the ones documented
below.

## Top-level metadata owned by `pkg`

These fields are synchronized from the directory layout during `UpdateConfig`
and are the only metadata fields automatically repaired by `--fix-config`:

- `name`
- `version`
- `localVersion`
- `only_portable`

Additional top-level keys that belong to the runtime model:

- `description`
- `homepage`
- `downloadURL`

## Runtime blocks

### `[[shortcut]]`

Accepted keys:

- `name` (required)
- `targetPath` (required)
- `arguments`
- `workingDirectory`
- `iconLocation`
- `description`

### `[[environment]]`

Accepted keys:

- `Name` (required)
- `Value` (required)

### `[[path]]`

Accepted key:

- `value` (required)

`pkg` stores PATH entries internally as `list[str]`, but the file format uses
repeated `[[path]]` tables.

### `[[bin]]`

Accepted keys:

- `name` (required)
- `content` (required)

## Variable expansion rules

### Package variables

These always expand:

- `$App`
- `$Icons`
- `$Shortcuts`

They expand relative to `<package_root>/current/...`.

This is intentional: package variables follow the active package view that
`Install` reasserts. Rerunning `Install` for the same version is a supported
repair path for shortcuts, environment variables, PATH entries, and wrapper
files, so those expansions stay anchored to `current` rather than a specific
version directory.

### Environment variables

- `${VAR}` expands everywhere and is treated as an error when unresolved.
- Plain non-package `$NAME` does not count as environment-variable syntax.
- Inside `[[bin]]` content, plain non-package `$NAME` stays literal so shell and
  PowerShell variables keep their native meaning.

### Escaping

- `$$` becomes a literal `$`.

## Missing `pkg.toml`

- `Install` uses runtime defaults and does not create the file.
- `UpdateConfig` creates a documented starter `pkg.toml` containing owned
  metadata fields plus commented examples for `[[shortcut]]`, `[[environment]]`,
  `[[path]]`, and `[[bin]]`.
- For an existing canonical `pkg.toml`, `UpdateConfig` syncs only the owned
  top-level metadata keys and preserves surrounding comments and unrelated
  content.
