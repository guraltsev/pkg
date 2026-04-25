# Architecture

## Goals of the refactor

The codebase now keeps the entire package-manager implementation in `pkg.py`
while still drawing a hard boundary between package-management policy and
Windows-specific side effects.

- `Shared models and pure helpers` decides shared data structures, validation,
  variable expansion, version comparison, and atomic file operations.
- `Windows integration boundary` exposes thin Python wrappers for direct
  Windows primitives.
- `Package-management logic and CLI` decides what should happen at a package
  level and orchestrates those wrappers through `WindowsPlatform`.
- `Script entry point` provides the executable handoff.

This layout keeps raw Windows commands and APIs easy to audit in one place
without letting package-management policy drift into the Windows wrapper
section.

## Section map

### `Shared models and pure helpers`

Pure/shared code:

- data models such as `PackageIdentity`, `PackageConfig`, `StepResult`
- TOML backend discovery and read helpers
- atomic file writers
- version comparison helpers
- variable expansion rules
- reporter and compatibility helpers

Imports for this section live immediately under its section header, matching the
single-file organization requirement.

### `Windows integration boundary`

Windows wrapper code:

- shortcut writers (`create_shortcut()`, `create_shortcut_with_pywin32()`,
  `create_shortcut_with_powershell()`)
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

- config normalization and validation
- package metadata facade
- metadata consistency checks
- round-trip/fallback metadata synchronization for `pkg.toml`
- install orchestrators such as `JunctionManager`, `ShortcutInstaller`,
  `EnvironmentVariableManager`, `PATHManager`, and `BinFileCreator`
- CLI parsing and high-level action orchestration

This section depends on the Windows wrapper functions rather than embedding
registry APIs, COM automation, or command execution details inline.

### `Script entry point`

A minimal `if __name__ == "__main__":` handoff that runs `main()`.

## Execution flow

### Install

1. CLI arguments are parsed in `main()`.
2. `PackageManager.install()` resolves the input path through
   `WindowsPlatform.resolve_input_path()`.
3. `PackageMetadata` loads and normalizes `pkg.toml`.
4. `PackageManager` validates portability and metadata consistency.
5. If needed, `WindowsPlatform.update_current_junction_if_needed()` runs the
   package-level junction policy and uses the raw junction wrappers to repoint
   `current`.
6. The install-step pipeline runs in order:
   - shortcuts
   - environment variables
   - ensuring the scope bin directory is on PATH
   - extra PATH entries
   - wrapper files

### UpdateConfig

1. CLI arguments are parsed in `main()`.
2. `PackageManager.update_config()` resolves the target version directory.
3. `PackageMetadata.update_config()` updates only owned metadata keys.
4. Existing TOML structure/comments are preserved when possible.
5. Bare metadata-only configs from the recent regression are upgraded to the
   richer documented starter template.
6. Missing `pkg.toml` files get a documented starter config with commented
   examples for the supported runtime sections.

## Why the section split matters

The original code mixed policy and mutation details in one large, undifferentiated
file. The new structure keeps the single-file requirement while still:

- making Windows side effects discoverable in one place
- keeping package logic easier to test on non-Windows hosts
- clarifying ownership of config metadata versus runtime content
- reducing the chance of accidentally introducing Windows-specific behavior into
  general package logic
