# Part 1 - compatibility, near-compatibility, legacy, and alias code

## Scope

Inspected:

- `pkg.py`
- `helper_scripts/legacy_to_pkg_toml.py`
- `pkg.cmd`
- `README.md`
- `docs/api.md`
- `docs/configuration.md`
- `tests/test_pkg_pure.py`
- `tests/test_quality.py`

Excluded from the core inventory:

- `.gitconfig/gitconfig.py` (repo-maintenance tooling, not `pkg` runtime behavior)

## Executive summary

The repo is already a single-file application, but it still carries a lot of code whose job is not "do the package work" but "keep older names, older shapes, older wrappers, or optional backends working".

The simplification target breaks down into five overlapping buckets:

1. **Schema aliases and old config shapes** (`env`, `shortcuts`, `path` alias inside `[[shortcut]]`, `[[main]]`, `portable`, `local_version`, etc.).
2. **Representation compatibility** (`PackageMetadata` as a compatibility facade, `PackageConfig -> dict -> typed spec` churn).
3. **Transitional legacy paths** (recent metadata-only upgrade logic, `pkg.json` cleanup, legacy JSON converter script).
4. **Optional-backend compatibility** (multiple TOML backends, multiple shortcut backends).
5. **Launcher / CLI compatibility** (deprecated flags, hidden passthrough args, wrapper-side interpreter-selection compatibility).

The highest-value simplification is to choose **one canonical `pkg.toml` schema** and **one runtime config model**, then delete the code that exists only to preserve older spellings or older shapes.

---

## 1. Explicit alias code and old-shape config handling

### In `pkg.py`

The main alias machinery is concentrated in these definitions:

- `TOP_LEVEL_CONFIG_KEY_ALIASES` - `pkg.py:2667-2686`
- `MAIN_TABLE_KEY_ALIASES` - `pkg.py:2687-2699`
- `ENVIRONMENT_KEY_ALIASES` - `pkg.py:2700`
- `BIN_KEY_ALIASES` - `pkg.py:2701`
- `SHORTCUT_KEY_ALIASES` - `pkg.py:2702-2716`
- `PATH_ENTRY_KEY_ALIASES` - `pkg.py:2717`
- `OWNED_METADATA_KEY_ALIASES` - `pkg.py:2720-2725`
- `PackageMetadata._canonicalize_dict_keys()` - `pkg.py:3005-3037`
- `PackageMetadata._canonicalize_config_dict()` - `pkg.py:3057-3169`
- `_find_existing_metadata_key()` - `pkg.py:3419-3442`
- `_sync_text_metadata()` alias-preserving regex replacement - `pkg.py:3482-3499`

What these aliases support today:

- top-level synonyms such as `env`, `shortcuts`, `portable`, `onlyportable`, `local_version`, `download_url`
- old `[[main]]` wrapping for package metadata
- shortcut entry synonyms such as `path`, `args`, `workdir`, `desc`
- path-entry alias `path -> value`
- metadata sync that preserves old spellings instead of rewriting canonical names

### Evidence that alias support is not just passive

- The behavior is encoded in tests: `tests/test_pkg_pure.py:349-375` intentionally feeds an alias-heavy config and asserts that it normalizes successfully.
- The documentation advertises alias support: `docs/configuration.md:40` and `docs/configuration.md:49`.

### Evidence that alias support is already causing real complexity / bugs

I reproduced this config:

```toml
[[main]]
portable = true
```

It fails with:

```text
ConfigValidationError
Duplicate keys differing only by case/alias in config: 'only_portable' and 'portable' both map to 'only_portable'.
```

That behavior comes directly from the alias machinery:

- top-level map treats `portable` as `only_portable` - `pkg.py:2682-2684`
- `[[main]]` map keeps `portable` as `portable` - `pkg.py:2696-2698`
- normalization runs through `_canonicalize_config_dict()` and flattens `main` into the top level - `pkg.py:3074-3097`
- `normalize_runtime_config()` canonicalizes twice - `pkg.py:2756-2761`

So the alias system is not only complexity; it already creates contradictory state.

### Associated alias code outside `pkg.py`

`helper_scripts/legacy_to_pkg_toml.py` carries a second alias-normalization surface:

- shortcut key aliases - `helper_scripts/legacy_to_pkg_toml.py:151-165`
- top-level aliases - `helper_scripts/legacy_to_pkg_toml.py:283-296`

This means the repo currently maintains **two separate alias systems**.

### Plan to completely remove alias handling

1. **Freeze one canonical schema and publish it clearly.**
   - Top-level metadata lives at the top level, not inside `[[main]]`.
   - Accept only canonical keys.
   - Recommended canonical block shapes:
     - `[[shortcut]]`: `name`, `targetPath`, `arguments`, `workingDirectory`, `iconLocation`, `description`
     - `[[environment]]`: `Name`, `Value`
     - `[[path]]`: `value`
     - `[[bin]]`: `name`, `content`

2. **Delete all alias maps and alias-preserving lookup.**
   - Remove `TOP_LEVEL_CONFIG_KEY_ALIASES`, `MAIN_TABLE_KEY_ALIASES`, `ENVIRONMENT_KEY_ALIASES`, `BIN_KEY_ALIASES`, `SHORTCUT_KEY_ALIASES`, `PATH_ENTRY_KEY_ALIASES`, and `OWNED_METADATA_KEY_ALIASES`.
   - Replace `_canonicalize_dict_keys()` and `_canonicalize_config_dict()` with a direct validator for exact keys only.
   - Delete `_find_existing_metadata_key()`.
   - Make metadata sync always write canonical key names.

3. **Update every in-repo config, fixture, example, and test to canonical names only.**
   - Remove alias-heavy test coverage from `tests/test_pkg_pure.py:349-375`.
   - Update `docs/configuration.md` to remove alias claims.

4. **Do not replace alias maps with a smaller alias map.**
   - The simplification win comes from deleting the alias concept, not trimming it.

---

## 2. Legacy config shape support: `[[main]]`

The code still supports the old `[[main]]` wrapper shape even though the starter config generator now emits top-level metadata keys.

### Where `[[main]]` is supported

- top-level alias map includes `main` - `pkg.py:2685`
- `PackageMetadata._canonicalize_config_dict()` flattens `main` into the top level - `pkg.py:3080-3097`
- `is_metadata_only_config_text()` accepts an optional `[[main]]` wrapper - `pkg.py:3327-3349`
- `locate_metadata_container()` explicitly searches for `main` - `pkg.py:3403-3416`
- `_sync_text_metadata()` has `[[main]]` parsing logic - `pkg.py:3464-3480`
- the legacy converter script emits `[[main]]` - `helper_scripts/legacy_to_pkg_toml.py:333-341`

### Why this is legacy, not a core feature

`create_starter_config()` already writes canonical top-level metadata:

- `pkg.py:3567-3578`

So the repo's own current output format no longer needs `[[main]]`.

### Plan to remove `[[main]]`

1. Stop accepting `[[main]]` in the runtime parser.
2. Stop special-casing `[[main]]` in `UpdateConfig`.
3. Remove `locate_metadata_container()` and the `main`-related branches in `_sync_text_metadata()`.
4. Delete the `[[main]]` output from `helper_scripts/legacy_to_pkg_toml.py` by deleting that script entirely (see legacy-script section below).
5. Update any existing fixtures or user configs to top-level metadata before landing the cleanup.

Once `[[main]]` is gone, a large chunk of config normalization and metadata-sync complexity disappears automatically.

---

## 3. Variable-expansion compatibility semantics

The repo still supports plain `$VAR` environment expansion in general config fields for compatibility.

### Evidence

- `expand_text()` explicitly supports plain `$VAR` in `ExpansionMode.GENERAL` - `pkg.py:897-980`, especially `pkg.py:905-907` and `pkg.py:959-975`
- `README.md:83-85` explicitly describes plain `$VAR` support as compatibility behavior
- `docs/configuration.md:87-90` documents the same split between `${VAR}` and plain `$VAR`

### Why this is compatibility code

The simpler and clearer rule is:

- package variables: `$App`, `$Icons`, `$Shortcuts`
- environment variables: `${VAR}` only

Supporting both `${VAR}` and plain `$VAR` adds branching and surprise, especially because wrapper/script fields intentionally behave differently.

### Plan to remove plain `$VAR` compatibility

1. Make `${VAR}` the only environment-variable syntax.
2. Keep package variables (`$App`, `$Icons`, `$Shortcuts`) because they are core to the tool.
3. Remove the plain `$VAR` expansion branch from `expand_text()`.
4. Update tests and fixtures to use `${VAR}` where they currently rely on plain env-variable expansion.
5. Update docs to remove the compatibility wording.

This is a small but meaningful simplification because it reduces parser ambiguity and makes the config rule set easier to explain.

---

## 4. Thin compatibility wrappers and facades

### 4.1 `VariableExpander`

Relevant code:

- `VariableExpander` - `pkg.py:460-506`

Evidence:

- the class docstring explicitly says it is a compatibility wrapper around `expand_text()` - `pkg.py:460-464`
- repo-wide search found no in-repo call sites beyond its own definition and API documentation

Plan:

- delete `VariableExpander`
- keep `expand_text()` as the single supported API
- remove `VariableExpander` from `docs/api.md:19`

### 4.2 Runtime/platform facades

The rest of this section covers the larger facade layer around package metadata and platform calls.

---

## 5. Representation compatibility and facade code

This is the biggest internal simplification target.

### The current compatibility/facade layer

- `PackageMetadata` explicitly calls itself a "Compatibility facade" - `pkg.py:2953-2954`
- `PackageMetadata.__init__()` duplicates identity data into many separate fields - `pkg.py:2966-2989`
- `PackageMetadata.load_config()` stores `runtime_config` but also explodes it back into dict/list shadows - `pkg.py:3181-3200`
- `package_config_to_dict()` converts `PackageConfig` back into a plain dict - `pkg.py:2830-2863`
- `read_runtime_config()` returns both a typed runtime model and a raw dict, and synthesizes a raw dict from defaults when no file exists - `pkg.py:2902-2950`

The compatibility shadows are then consumed by the install pipeline:

- `ShortcutInstaller._prepare_shortcut()` rebuilds a `ShortcutSpec` from `metadata.shortcut` dicts - `pkg.py:1871-1893`
- `EnvironmentVariableManager.install_environment_variables()` consumes `metadata.environment` dicts - `pkg.py:2074-2098`
- `PATHManager.add_to_path()` is fed `metadata.path` list shadows through `install_extra_path_entries_step()` - `pkg.py:2481-2494`
- `BinFileCreator.create_wrapper()` consumes `metadata.bin` dicts - `pkg.py:2329-2398` and `pkg.py:2401-2425`

### Evidence that this is compatibility churn, not necessary domain modeling

- `package_config_to_dict()` has only one in-repo call site: `pkg.py:2948`.
- `PackageMetadata.load_config()` immediately converts the typed runtime config back into dict/list shadows - `pkg.py:3181-3200`.
- Tests still use those shadow surfaces directly:
  - `tests/test_pkg_pure.py:471` accesses `metadata.bin[0]["content"]`
  - `tests/test_pkg_pure.py:502` assigns `metadata.shortcut = [...]`

### What should remain after cleanup

Keep the real domain models:

- `PackageIdentity`
- `PackageConfig`
- `ShortcutSpec`
- `EnvVarSpec`
- `BinSpec`
- `ScopePaths`

### Plan to remove the compatibility facade behavior

1. **Make `PackageConfig` the only runtime config model.**
   - Install code should read from `metadata.runtime_config` directly.
   - Stop populating `metadata.environment`, `metadata.bin`, `metadata.path`, and `metadata.shortcut` shadows.

2. **Keep `PackageMetadata` only if it still reduces parameter noise.**
   - If kept, reduce it to concrete install state such as:
     - `identity`
     - `runtime_config`
     - `scope`
     - `scope_paths`
   - Remove duplicated mirror fields that just repeat `identity` or `runtime_config`.

3. **Delete `package_config_to_dict()`.**
   - For "no config file" cases, return `raw_config = None` or `{}` for consistency checks instead of fabricating a raw dict from defaults.

4. **Delete the `PackageMetadata.check_metadata_consistency()` wrapper method.**
   - Call `check_metadata_consistency(identity, raw_config)` directly.

5. **Move installers to typed inputs.**
   - `ShortcutInstaller` should take `ShortcutSpec` values directly.
   - `EnvironmentVariableManager` should take `EnvVarSpec` values directly.
   - `BinFileCreator` should take `BinSpec` values directly.

This is the core simplification because it removes the "old shape vs new shape" churn inside the install path.

---

## 6. Transitional legacy and regression-repair code

### 6.1 Metadata-only config upgrade logic

This is temporary code for a recent regression, not a permanent product feature.

Evidence:

- `is_metadata_only_config_text()` says it only exists to identify a recent regression - `pkg.py:3315-3360`
- `PackageMetadata.update_config()` has a dedicated branch that upgrades metadata-only files to a starter template - `pkg.py:3221-3233`

### Plan

1. Migrate any known metadata-only generated files once.
2. Remove `is_metadata_only_config_text()`.
3. Remove the special-case update path in `PackageMetadata.update_config()`.
4. Keep only two `UpdateConfig` behaviors:
   - update canonical metadata in an existing canonical `pkg.toml`
   - create a new starter config when `pkg.toml` is missing

### 6.2 `pkg.json` cleanup

Evidence:

- `PackageMetadata.update_config()` still deletes `pkg.json` as a legacy format - `pkg.py:3218` and `pkg.py:3251-3258`

### Plan

1. If `pkg.json` migration is still needed, do it in a one-time external migration step.
2. Then remove the `pkg.json` deletion code from `UpdateConfig`.
3. Do not keep long-term runtime code just to clean up a file format the tool no longer reads.

### 6.3 Legacy JSON conversion script

`helper_scripts/legacy_to_pkg_toml.py` is legacy from top to bottom.

Evidence:

- file docstring explicitly says it converts legacy package config files - `helper_scripts/legacy_to_pkg_toml.py:1-5`
- it scans for legacy JSON files by name and prefix - `helper_scripts/legacy_to_pkg_toml.py:99-137`
- it rewrites legacy shortcut keys - `helper_scripts/legacy_to_pkg_toml.py:140-179`
- it rewrites legacy package-variable spellings - `helper_scripts/legacy_to_pkg_toml.py:206-232`
- it reads `opt_pkg.json` and related JSON files - `helper_scripts/legacy_to_pkg_toml.py:279-310`
- it still emits `[[main]]` - `helper_scripts/legacy_to_pkg_toml.py:333-341`

### Plan

1. Delete `helper_scripts/legacy_to_pkg_toml.py`.
2. Remove any docs or tests that mention it. Repo-wide search found no production callers; the only in-repo reference is from `tests/test_quality.py:11-14`.
3. Do not replace it with a smaller converter inside `pkg.py`; that would reintroduce the same problem.

---

## 7. Optional-backend compatibility code

### 7.1 TOML backend compatibility

Relevant code:

- `TomlReader` - `pkg.py:102-111`
- `RoundTripBackend` - `pkg.py:115-125`
- `TextConfigDocument` fallback wrapper - `pkg.py:129-150`
- `try_import()` - `pkg.py:509-523`
- `load_toml_reader()` - `pkg.py:694-719`
- `load_roundtrip_toml_backend(require)` - `pkg.py:722-739`
- `load_config_document()` - `pkg.py:3363-3387`
- `sync_document_metadata()` fallback split - `pkg.py:3513-3534`

Why this belongs in the inventory:

- `TextConfigDocument` explicitly says it keeps the interface compatible with `tomlkit`'s `as_string()` API - `pkg.py:132-135`
- `load_roundtrip_toml_backend()` explicitly says `require` is a retained compatibility parameter - `pkg.py:725-728`

### Plan

The simplest concrete policy is:

- require Python 3.11+ and use stdlib `tomllib` for reading
- require `tomlkit` for `UpdateConfig` round-trip editing

That lets you delete:

- `TomlReader`
- `RoundTripBackend`
- the TOML-backend branching in `read_toml_file()`
- `TextConfigDocument`
- the `require` parameter and wrapper in `load_roundtrip_toml_backend()`
- fallback text-mutation code in `_sync_text_metadata()` / `load_config_document()`

If you do **not** want a `tomlkit` dependency, the other simple choice is even more direct: stop promising round-trip preservation and rewrite the canonical metadata block in a plain-text way. But do not keep the current dual-path abstraction.

### 7.2 Shortcut backend compatibility

Relevant code:

- `get_win32com_client()` - `pkg.py:998-1006`
- `get_shortcut_backend()` - `pkg.py:1024-1031`
- `create_shortcut_with_pywin32()` - `pkg.py:1065-1087`
- `create_shortcut_with_powershell()` - `pkg.py:1090-1119`
- `create_shortcut()` - `pkg.py:1122-1142`
- `ShortcutInstaller._create_shortcut_with_pywin32()` - `pkg.py:1896-1918`
- `ShortcutInstaller._create_shortcut_with_powershell()` - `pkg.py:1921-1943`
- backend selection warning - `pkg.py:1995-1999`

### Plan

Choose one backend and delete the other path.

For a simple, no-extra-dependency tool, **PowerShell-only** is the cleaner choice:

- delete `get_win32com_client()`
- delete `get_shortcut_backend()`
- delete `create_shortcut_with_pywin32()`
- delete `ShortcutInstaller._create_shortcut_with_pywin32()`
- delete the `backend` parameter plumbing
- keep one concrete shortcut-creation path

If your environment strongly prefers `pywin32`, then make `pywin32` required and delete the PowerShell path instead. The important simplification rule is: **do not keep both**.

---

## 8. Platform facade and launcher compatibility

### 8.1 Windows facade code

Relevant code:

- `WindowsPlatform` - `pkg.py:2524-2578`
- `DEFAULT_PLATFORM` - `pkg.py:2581`
- top-level `pause_if_requested()` wrapper - `pkg.py:2584-2591`
- optional `platform` injection in `PackageMetadata.__init__()` - `pkg.py:2956-2963`
- optional `platform` injection in `PackageManager.__init__()` - `pkg.py:3626-3644`

Why this is near-compatibility rather than core logic:

- `WindowsPlatform` is a thin delegate layer over existing functions/managers.
- The user-visible behavior does not depend on the class existing; it mainly exists as an indirection seam.

### Plan

1. Remove `WindowsPlatform`.
2. Remove `DEFAULT_PLATFORM`.
3. Inline pause handling in `main()`.
4. Call concrete functions/managers directly from `PackageManager` and `PackageMetadata`.
5. Patch concrete functions in tests instead of patching a facade object.

### 8.2 Deprecated and hidden CLI compatibility

Relevant code:

- deprecated constructor flag `no_autoupdate_config` - `pkg.py:3626-3656`
- CLI flag `--no-autoupdate-config` - `pkg.py:4017-4021`
- hidden `--python` arg - `pkg.py:3990-3994`

Evidence:

- `PackageManager.__init__()` explicitly labels `no_autoupdate_config` deprecated compatibility - `pkg.py:3641-3642`
- `--python` is hidden and otherwise unused inside `pkg.py`; it exists to tolerate wrapper forwarding

### Plan

1. Delete `no_autoupdate_config` from the constructor.
2. Delete `--no-autoupdate-config` from the CLI.
3. Decide whether `pkg.cmd` should keep interpreter-override compatibility. If not, delete hidden `--python` from `pkg.py` too.

### 8.3 Wrapper-side interpreter-selection compatibility

Relevant repo-level code:

- `pkg.cmd` interpreter override and fallback chain - `pkg.cmd:7-12` and `pkg.cmd:29-57`

This is not the core Python package manager, but it is still compatibility logic in the repo.

### Plan

If you want the repo itself to be simpler, collapse `pkg.cmd` to one straightforward interpreter strategy and remove the wrapper-side override matrix. If you keep it, strip wrapper-only args before invoking `pkg.py` so the Python CLI does not have to carry hidden passthrough flags.

---

## 9. Associated documentation and test surfaces that must change with the cleanup

These are not the primary target of Part 1, but they are part of the compatibility surface and must be updated or deleted when the code is simplified:

- `docs/api.md:19` - mentions `VariableExpander`
- `docs/api.md:31` - mentions `package_config_to_dict()`
- `docs/api.md:39` - mentions `WindowsPlatform`
- `docs/api.md:61-76` - explicit "Compatibility expectations" section
- `docs/configuration.md:40` and `docs/configuration.md:49` - alias and case-insensitive key claims
- `README.md:83-85` - plain `$VAR` compatibility claim
- `README.md:114-115` - `tomlkit` fallback claim
- `tests/test_pkg_pure.py:349-375` - alias support test
- `tests/test_pkg_pure.py:471` and `tests/test_pkg_pure.py:502` - tests tied to dict-shadow compatibility surfaces

---

## 10. Recommended order of removal

### Phase 1 - Freeze the canonical user-facing schema

- canonical top-level metadata only
- no `[[main]]`
- no alias spellings
- `${VAR}` only for environment variables

### Phase 2 - Remove alias and old-shape parsing

Touch:

- `pkg.py:2667-3169`
- `pkg.py:3315-3534`
- `docs/configuration.md`
- `tests/test_pkg_pure.py` alias tests

### Phase 3 - Collapse runtime representations

Touch:

- `pkg.py:2728-2950`
- `pkg.py:2953-3201`
- install managers in `pkg.py:1871-2425`
- tests that currently poke dict shadows

### Phase 4 - Remove transitional legacy code

Touch:

- `pkg.py:3203-3262`
- `pkg.py:3315-3360`
- delete `helper_scripts/legacy_to_pkg_toml.py`

### Phase 5 - Remove optional-backend and facade compatibility

Touch:

- TOML loader/update path in `pkg.py:102-150`, `pkg.py:509-523`, `pkg.py:694-739`, `pkg.py:3363-3534`
- shortcut backend path in `pkg.py:998-1142`, `pkg.py:1896-1999`
- platform facade path in `pkg.py:2524-2591`, `pkg.py:2956-2963`, `pkg.py:3626-3644`
- CLI args in `pkg.py:3990-4021`
- optionally simplify `pkg.cmd`

---

## 11. What the code should look like after the cleanup

After the cleanup, the core flow should be much smaller and more direct:

1. Read canonical `pkg.toml` with one clear parser path.
2. Validate exact keys and exact shapes.
3. Produce one runtime model: `PackageConfig`.
4. Install directly from typed specs.
5. Update metadata in one canonical config shape.
6. No legacy JSON, no alias maps, no `[[main]]`, no compatibility facade, no duplicate backend ladders.

That is the version of the code that best matches the stated goals:

- one main Python file
- simple configuration
- simple code
- no generic abstraction
- no future-proofing
- organized in sections, but not layered around old shapes
