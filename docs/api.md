# API reference

## Public entry point

### `pkg.py`

`pkg.py` contains the full implementation and exposes both the command-line
entry point and the importable public API.

## Shared/public types and helpers

Notable public types and helpers defined in the shared section:

- `Scope`, `Action`
- `PackageIdentity`, `ShortcutSpec`, `EnvVarSpec`, `BinSpec`, `PackageConfig`, `ScopePaths`
- `StepResult`, `ActionResult`, `ExpansionResult`
- `compare_package_versions()`
- `expand_text()`
- `read_toml_file()`
- `write_text_atomic()` / `write_bytes_atomic()`

## Package-management API

Notable public API defined in the package-management section:

- `resolve_input_path()`
- `compute_scope_paths()`
- `normalize_runtime_config()`
- `validate_runtime_config()`
- `check_metadata_consistency()`
- `read_runtime_config()`
- `sync_config_metadata_text()`
- `create_starter_config()`
- `JunctionManager`
- `ShortcutInstaller`
- `EnvironmentVariableManager`
- `PATHManager`
- `BinFileCreator`
- `PackageMetadata`
- `PackageManager`
- `main()`

## Windows integration API

Notable public API defined in the Windows integration section:

- `create_shortcut()`
- `create_junction()`
- `is_junction()`
- `get_junction_target()`
- `read_registry_value()`
- `write_registry_value()`
- `broadcast_environment_change()`
- `is_current_user_admin()`
- `wait_for_keypress()`

## Notes for callers

The runtime config surface is the typed `PackageConfig` model. `PackageMetadata`
keeps the directory-derived package identity plus the loaded typed runtime
config. `UpdateConfig` only synchronizes the canonical top-level metadata keys
and does not perform alias rewriting or legacy-shape upgrades.
