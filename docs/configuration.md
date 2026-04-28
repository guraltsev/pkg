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

`name` is expanded before placement. It may be:

- a simple output name such as `Ripgrep`
- a nested relative path under the default shortcut root such as `Tools\Ripgrep`
- a path-like destination that resolves outside the default shortcut root

Absolute paths and escaping parent traversal are intentionally allowed. This is
powerful and sometimes useful, but package authors should use it sparingly.
When install detects that the final shortcut path lands outside the default
scope shortcut root, it prints a warning and still creates the shortcut.

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

`name` follows the same placement rule as `[[shortcut]].name`: expansion runs
first, then the result may be a simple file name, a nested relative path under
the default bin root, or a path-like destination outside that root.

Absolute paths and escaping parent traversal are intentionally allowed here as
well. Install warns when the final wrapper path lands outside the default bin
root, but it does not block creation.

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
