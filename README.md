# pkg

`pkg` is a Windows package tool for locally cached applications.

The implementation now lives entirely in `pkg.py`. The file is intentionally
organized into clearly labeled sections so the architecture stays easy to audit
without reintroducing separate implementation modules:

- `Shared models and pure helpers`
- `Windows integration boundary`
- `Package-management logic and CLI`
- `Script entry point`

The section layout preserves a strict separation of concerns: the
`Windows integration boundary` section now contains thin Python wrappers around
direct Windows primitives such as shortcut creation, registry reads/writes,
junction creation, privilege checks, and environment-change broadcasts, while
package-management orchestration lives in the `Package-management logic and
CLI` section.

## Documentation

Documentation is intentionally redundant and easy to find:

- [`README.md`](README.md) — user overview and quick start
- [`docs/README.md`](docs/README.md) — documentation index
- [`docs/architecture.md`](docs/architecture.md) — section boundaries and execution flow
- [`docs/configuration.md`](docs/configuration.md) — `pkg.toml` schema and variable rules
- [`docs/api.md`](docs/api.md) — public API and developer reference
- [`docs/review.md`](docs/review.md) — strengths, weaknesses, and refactor summary

In addition, every function and class in the Python codebase has an in-code
module/class/function docstring.

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

You can run `pkg` from a version directory, a `current` junction, or the
package root.

## Actions

- `Install` is repair-oriented: rerunning it for the same version is
  intentional and reapplies package state so broken shortcuts, environment
  variables, PATH entries, and wrapper files can be restored. It may also
  recreate the `current` junction.
- `UpdateConfig` creates a documented starter `pkg.toml` with commented
  examples when missing, upgrades recent metadata-only auto-generated configs,
  or syncs only the filesystem-derived metadata in an existing `pkg.toml`.

## Exit codes

- `0` success, including “no changes needed”
- `2` user/config/input/dependency problem
- `3` system mutation failure
- `4` unexpected internal failure

## Config notes

`Install` does not auto-create `pkg.toml` when it is missing.

When `pkg.toml` is missing, `UpdateConfig` now creates a self-documenting file
that includes the synchronized metadata plus commented examples for shortcuts,
environment variables, PATH entries, and wrapper scripts.

For an existing `pkg.toml`, `UpdateConfig` preserves comments, unknown keys, and
existing layout while syncing only these owned metadata keys:

- `name`
- `version`
- `localVersion`
- `only_portable`

`${VAR}` is the recommended environment-expansion syntax. Plain `$VAR` remains
supported in general config fields for compatibility, but wrapper script content
does not treat plain `$VAR` as a `pkg` expansion unless it is a package
variable such as `$App`.

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

`tomlkit` is preferred for round-trip TOML editing. When it is unavailable,
`pkg` falls back to a narrow metadata-only updater for `UpdateConfig` so
existing files can still be synced without broad rewrites.
