# Architecture

## Goals of the cleanup

The codebase now keeps the entire package-manager implementation in `pkg.py`
while also removing the compatibility layers that used to preserve older config
spellings, older config shapes, and optional backend ladders.

The resulting design is intentionally direct:

- one canonical `pkg.toml` schema
- one runtime model: `PackageConfig`
- one shortcut backend path
- one TOML read path
- one metadata-sync path for `UpdateConfig`

## Section map

### `Shared models and pure helpers`

Pure and shared code:

- data models such as `PackageIdentity`, `ShortcutSpec`, `EnvVarSpec`,
  `BinSpec`, `PackageConfig`, and `StepResult`
- stdlib TOML loading helpers
- atomic file writers
- version comparison helpers
- variable expansion rules
- module-level stdout logging helpers

Imports for this section live immediately under its section header, matching the
single-file organization requirement.

### `Windows integration boundary`

Windows wrapper code:

- shortcut creation through a single PowerShell-based writer
- junction primitives (`create_junction()`, `is_junction()`,
  `get_junction_target()`)
- registry wrappers (`read_registry_value()`, `write_registry_value()`)
- environment-change broadcast, admin detection, and keypress pause wrappers

This section intentionally stays free of package-management orchestration. It
contains the direct Windows commands and API calls, while the higher-level
classes that decide when to use them live below in the package-management
section.

### `Package-management logic and CLI`

Package-management logic:

- path resolution and scope-path calculation
- exact-key config normalization and validation
- metadata consistency checks
- `PackageMetadata` and `PackageManager`
- install-step orchestration classes:
  - `JunctionManager`
  - `ShortcutInstaller`
  - `EnvironmentVariableManager`
  - `PATHManager`
  - `BinFileCreator`
- CLI parsing and action dispatch

### `Script entry point`

A minimal `if __name__ == "__main__":` handoff that runs `main()`.

## Execution flow

### Install

1. CLI arguments are parsed in `main()`.
2. `PackageManager.install()` resolves the input path with `resolve_input_path()`.
3. `PackageMetadata` loads `pkg.toml` through `read_runtime_config()` and stores
   the typed `PackageConfig` model.
4. `PackageManager` collects load warnings and checks raw top-level metadata for
   directory/config mismatches.
5. When `--fix-config` is enabled and owned metadata is stale, install runs
   `PackageMetadata.update_config()`, reloads `pkg.toml`, and replaces the
   in-memory runtime config before continuing.
6. Only after that repair/reload step does install derive effective
   `only_portable` and apply the Machine-scope portability gate.
7. If needed, `JunctionManager.update_current_junction_if_needed()` repoints the
   package-level `current` junction.
8. The install-step pipeline runs in order:
   - shortcuts
   - environment variables
   - ensuring the scope bin directory is on PATH
   - extra PATH entries
   - wrapper files

The pipeline is repair-oriented. Same-version reinstalls are not treated as a
no-op; the steps still run so broken shortcuts, environment variables, PATH
entries, and wrapper files can be restored.

### UpdateConfig

1. CLI arguments are parsed in `main()`.
2. `PackageManager.update_config()` resolves the target version directory.
3. `PackageMetadata.update_config()` either:
   - creates a documented starter config when `pkg.toml` is missing, or
   - syncs only the canonical top-level metadata keys in an existing canonical
     `pkg.toml`
4. Existing comments and unrelated content are preserved when possible, and the
   rendered text is parsed again before it is written back out.

## Bootstrap interpreter contract

`pkg.cmd` is the bootstrap launcher for `pkg.py`. It chooses Python in this
order: `--python`, `PKG_PYTHON`, `pkg.python`, then `python` from `PATH`.
`pkg.py` intentionally keeps a hidden `--python` parser entry so the launcher
can forward that choice unchanged on systems where Python is not otherwise
discoverable in the usual way.

## Why the section split matters

The original code mixed policy and mutation details in one large,
undifferentiated file. The current structure keeps the single-file requirement
while also:

- making Windows side effects discoverable in one place
- keeping package logic easier to test on non-Windows hosts
- removing alias and facade churn from the install path
- clarifying ownership of config metadata versus runtime content
