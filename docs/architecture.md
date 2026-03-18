# Architecture

## Goals of the refactor

The codebase now keeps the entire package-manager implementation in `pkg.py`
while still drawing a hard boundary between package-management policy and
Windows-specific side effects.

- `Shared models and pure helpers` decides shared data structures, validation,
  variable expansion, version comparison, and atomic file operations.
- `Windows integration boundary` performs all direct Windows interaction.
- `Package-management logic and CLI` decides what should happen at a package
  level and delegates platform mutations through `WindowsPlatform`.
- `Script entry point` provides the executable handoff.

This layout keeps Start Menu shortcut creation, registry mutation, junction
management, and wrapper installation self-contained and easy to audit without
spreading the implementation across multiple files.

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

Windows integration code:

- current-junction inspection and replacement
- shortcut creation (`pywin32` or PowerShell)
- registry-backed environment variable writes
- registry-backed PATH updates
- wrapper file installation in the scope bin directory
- scope path resolution
- Administrator detection and console pause handling

Every direct Windows primitive lives here, including the imports needed for
subprocess execution, registry access, and low-level Windows calls.

### `Package-management logic and CLI`

Package-management logic:

- config normalization and validation
- package metadata facade
- metadata consistency checks
- round-trip/fallback metadata synchronization for `pkg.toml`
- CLI parsing and high-level action orchestration

This section depends on the `WindowsPlatform` facade rather than directly on
registry APIs, shortcut backends, PowerShell command execution, or junction
manipulation details.

### `Script entry point`

A minimal `if __name__ == "__main__":` handoff that runs `main()`.

## Execution flow

### Install

1. CLI arguments are parsed in `main()`.
2. `PackageManager.install()` resolves the input path through
   `WindowsPlatform.resolve_input_path()`.
3. `PackageMetadata` loads and normalizes `pkg.toml`.
4. `PackageManager` validates portability and metadata consistency.
5. If needed, `WindowsPlatform.update_current_junction_if_needed()` repoints the
   package `current` junction.
6. The Windows install-step pipeline runs in order:
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
5. Missing `pkg.toml` files get a minimal starter config.

## Why the section split matters

The original code mixed policy and mutation details in one large, undifferentiated
file. The new structure keeps the single-file requirement while still:

- making Windows side effects discoverable in one place
- keeping package logic easier to test on non-Windows hosts
- clarifying ownership of config metadata versus runtime content
- reducing the chance of accidentally introducing Windows-specific behavior into
  general package logic
