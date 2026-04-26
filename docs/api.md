# API reference

## Public entry point

### `pkg.py`

`pkg.py` contains the full implementation and exposes the command-line entry
point plus the importable public API.

## Shared/public types and helpers

Notable public types and helpers defined in the shared section:

- `Scope`, `Action`
- `PackageIdentity`, `PackageConfig`, `ScopePaths`
- `StepResult`, `ActionResult`
- `Reporter`
- `compare_package_versions()`
- `expand_text()` / `VariableExpander`
- `read_toml_file()`
- `write_text_atomic()` / `write_bytes_atomic()`

## Package-management API

Notable public API defined in the package-management section:

- `resolve_input_path()`
- `compute_scope_paths()`
- `normalize_runtime_config()`
- `validate_runtime_config()`
- `package_config_to_dict()`
- `check_metadata_consistency()`
- `read_runtime_config()`
- `JunctionManager`
- `ShortcutInstaller`
- `EnvironmentVariableManager`
- `PATHManager`
- `BinFileCreator`
- `WindowsPlatform`
- `DEFAULT_PLATFORM`
- `PackageMetadata`
- `PackageManager`
- `main()`

## Windows integration API

Notable public API defined in the Windows integration section:

- `create_shortcut()`
- `create_shortcut_with_pywin32()`
- `create_shortcut_with_powershell()`
- `create_junction()`
- `is_junction()`
- `get_junction_target()`
- `read_registry_value()`
- `write_registry_value()`
- `broadcast_environment_change()`
- `is_current_user_admin()`
- `wait_for_keypress()`

## Compatibility expectations

Existing callers and tests continue to rely on the following names from
`pkg.py`:

- `PackageManager`
- `PackageMetadata`
- `JunctionManager`
- `ShortcutInstaller`
- `EnvironmentVariableManager`
- `PATHManager`
- `BinFileCreator`
- `compare_package_versions`
- `expand_text`
- `resolve_input_path`
- `write_bytes_atomic`
- `main`
