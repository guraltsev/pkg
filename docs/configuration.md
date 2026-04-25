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

## Top-level metadata owned by `pkg`

These fields are synchronized from the directory layout during `UpdateConfig`
and are the only metadata fields automatically repaired by `--fix-config`:

- `name`
- `version`
- `localVersion`
- `only_portable`

## Runtime blocks

### `[[shortcut]]`

Canonical keys:

- `name` (required)
- `targetPath` (required)
- `arguments`
- `workingDirectory`
- `iconLocation`
- `description`

Supported aliases include `path`, `args`, `workdir`, and `desc`.

### `[[environment]]`

Canonical keys:

- `Name` (required)
- `Value` (required)

Keys are accepted case-insensitively and normalized to the canonical spellings.

### `[[path]]`

Canonical key:

- `value` (required)

The runtime model stores PATH entries internally as `list[str]`, regardless of
whether the TOML used repeated tables or other accepted convenience forms.

### `[[bin]]`

Canonical keys:

- `name` (required)
- `content` (required)

## Variable expansion rules

### Package variables

These always expand:

- `$App`
- `$Icons`
- `$Shortcuts`

They expand relative to `<package_root>/current/...`.

### Environment variables

- `${VAR}` expands everywhere and is treated as an error when unresolved.
- Plain `$VAR` expands only in general config fields.
- Plain `$VAR` stays literal inside wrapper content so PowerShell and similar
  script languages keep their native variable syntax.

### Escaping

- `$$` becomes a literal `$`.

## Missing `pkg.toml`

- `Install` uses runtime defaults and does not create the file.
- `UpdateConfig` creates a documented starter `pkg.toml` containing owned
  metadata fields plus commented examples for `[[shortcut]]`, `[[environment]]`,
  `[[path]]`, and `[[bin]]`.
- If a package still has one of the recent metadata-only auto-generated files,
  `UpdateConfig` upgrades it to the richer documented template while keeping a
  `.bak` backup.
