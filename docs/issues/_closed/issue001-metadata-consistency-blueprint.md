# Blueprint: fix metadata consistency enforcement in `Install`

Date: 2026-04-25
Priority: Critical

## Goal

Make `Install` validate metadata consistency against the **raw canonicalized contents of `pkg.toml`**, not against a reconstructed runtime representation that already rewrites directory-owned metadata.

This change must:

- catch real `name` / `version` / `localVersion` drift in `pkg.toml`
- keep the single-file structure
- avoid unrelated refactors
- avoid adding a new abstraction layer
- preserve current behavior for valid configs

## Why this is the core issue

The current bug is not “the comparison logic is wrong.” The comparison logic is mostly fine.

The real problem is that the install path loses the original file-authored metadata before it performs the consistency check.

That makes the current check look correct while silently validating the wrong representation.

## Evidence

### Expected behavior from docs

`docs/architecture.md:76-80` says install flow is:

1. parse CLI
2. resolve path
3. load and normalize `pkg.toml`
4. validate portability and metadata consistency

So the contract is clear: `Install` is expected to validate metadata consistency before mutating system state.

### Evidence from the mismatch fixture

`tests/fixtures/MismatchApp/v2.0.0.l3/pkg.toml:1-12` intentionally contains stale metadata:

- `name = "MismatchApp-OLD"`
- `version = "1.9.9"`
- `localVersion = 7`

That fixture demonstrates the exact kind of drift the install path should reject unless `--fix-config` is used.

### Evidence from the load path

`read_runtime_config()` already produces the right data shape:

- normalized runtime config
- canonicalized raw dict
- warnings

Code: `pkg.py:2938-2965`

But `PackageMetadata.load_config()` discards the raw dict:

- `config, _raw_data, warnings = ...`
- returns `package_config_to_dict(config, self.identity)`

Code: `pkg.py:3359-3389`

`package_config_to_dict()` then substitutes directory-derived metadata for the file-authored metadata:

- `name = identity.name`
- `version = identity.version`
- `localVersion = identity.local_version`

Code: `pkg.py:2857-2890`

`Install` validates consistency against that reconstructed dict:

- `config_data, load_warnings = metadata.load_config(...)`
- `inconsistencies = metadata.check_metadata_consistency(config_data)`

Code: `pkg.py:3929-3958`

`check_metadata_consistency()` also contains a second, weaker version of the same mistake: when passed a `PackageConfig`, it again synthesizes metadata from directory identity instead of from file-authored data.

Code: `pkg.py:2893-2935`

## Root cause

There are too many config representations in the install path, and the wrong one is treated as authoritative for validation.

The problem chain is:

1. raw TOML is loaded
2. canonicalized raw dict is created
3. normalized runtime config is created
4. a third “compatibility dict” is reconstructed from the runtime config
5. the consistency check runs against that reconstructed dict

The metadata drift disappears at step 4.

That is the core source of the bug.

## Scope of change

Touch only these areas:

- `read_runtime_config()` — `pkg.py:2938-2965`
- `check_metadata_consistency()` — `pkg.py:2893-2935`
- `PackageMetadata.load_config()` — `pkg.py:3359-3389`
- `PackageManager.install()` — `pkg.py:3929-3958`
- tests that cover mismatch handling and `--fix-config`

Do **not** touch:

- variable expansion
- PATH logic
- shortcut creation
- wrapper generation
- reinstall semantics
- `UpdateConfig` template generation

## Implementation plan

### Step 1: make raw canonicalized config the validation input

Keep `read_runtime_config()` as the single place that returns:

- `runtime_config`
- `raw_config`
- `warnings`

No new loader should be introduced.

### Step 2: stop discarding raw config in `PackageMetadata.load_config()`

Change `PackageMetadata.load_config()` so it preserves the canonicalized raw dict and returns that raw dict to callers.

Recommended shape:

- set `self.runtime_config = runtime_config`
- populate compatibility fields on the object from `runtime_config` as today
- return `(raw_config, warnings)`

This is the smallest fix because the raw dict already exists.

### Step 3: tighten the contract of `check_metadata_consistency()`

Preferred option:

- make `check_metadata_consistency()` accept only a raw dict
- remove the `PackageConfig` branch entirely

Fallback option if call sites make removal awkward:

- keep the function name
- raise `TypeError` for `PackageConfig`
- update callers accordingly

The important rule is: **owned metadata consistency must only be checked against raw file-derived data**.

### Step 4: keep install validation before mutations

`PackageManager.install()` should continue to validate consistency before any mutation step runs.

The only change is the representation being validated.

### Step 5: keep `--fix-config` behavior narrow

If inconsistencies are found:

- without `--fix-config`: abort before mutation
- with `--fix-config`: repair only the owned metadata keys and then continue

Do not broaden the automatic repair scope beyond:

- `name`
- `version`
- `localVersion`
- `only_portable`

## Why this is the best approach

This plan fixes the exact point where information is lost.

It is better than:

- reparsing `pkg.toml` later in `Install`
- storing stale file metadata inside `PackageConfig`
- adding yet another “source-of-truth” object
- special-casing mismatch fixtures in tests

Those options either duplicate parsing, widen the model unnecessarily, or hide the real issue.

## Test plan

### Add or update tests for these cases

1. **Direct mismatch detection**
   - Load `MismatchApp` raw config
   - Assert that metadata consistency reports the three mismatches

2. **Install aborts on mismatch without `--fix-config`**
   - Stub Windows-mutating functions
   - Assert install fails before component installation starts

3. **Install repairs mismatch with `--fix-config`**
   - Stub Windows-mutating functions
   - Assert metadata is repaired and install proceeds

4. **Matching config still passes**
   - Use `GoodApp`
   - Assert no mismatches are reported

5. **Missing `pkg.toml` with defaults does not create a false mismatch**
   - Cover `use_defaults=True`
   - Assert no bogus metadata inconsistency is raised for the generated defaults path

## Success criteria

- `Install` rejects stale `name` / `version` / `localVersion` in `pkg.toml` unless `--fix-config` is supplied
- the check runs before mutation
- the fix changes only the config-loading / validation path
- no new config representation is introduced
- valid packages behave exactly as before

## Non-goals

- no module split
- no `PackageConfig` redesign
- no PATH or wrapper refactor
- no change to same-version reinstall policy
- no attempt to “future-proof” config loading beyond the current repo needs

