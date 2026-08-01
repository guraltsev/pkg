# Blueprint: populate `App` from package origin

> **Architecture note:** The single-file constraint in this blueprint has been
> superseded. Origin behavior now lives in `src/pkg/origin.py`, while
> `src/pkg/pkg.py` retains the high-level action workflow.

Date: 2026-07-08
Priority: High
Change type: Feature design

## Goal

Allow `pkg` to install a package version even when the version directory does
not already contain a populated `App/` directory.

The feature adds an explicit origin step before the existing component install
sequence. The origin step can either:

- download a zip archive from `[origin].url` and unpack it into `App/`
- run a package-authored script that is responsible for populating `App/`

This makes the download URL meaningful while keeping the install pipeline
simple and repair-oriented.

## Design summary

Add a new `[origin]` table to `pkg.toml`.

The built-in origin mode downloads a zip file, optionally verifies a checksum,
extracts it to a temporary staging directory, selects either the archive root or
a declared subdirectory, and copies that selected content into `App/`.

The script origin mode executes a package-local script and pipes enriched
package metadata to the script as JSON on stdin. The script is responsible for
creating or populating `App/`.

Origin population runs by default only when `App/` is missing or empty. A new
explicit CLI option clears `App/` and repopulates it from origin.

## Definitions

For this feature, **pkg root** means the resolved package version directory:

```text
<PackageName>/<version>.l<localVersion>/
```

This is the directory that contains `pkg.toml` and owns `App/`, `Icons/`, and
`Shortcuts/`.

This definition is intentional because the existing code also uses
`package_root` to mean the parent directory that contains version directories
and `current`. Origin scripts must be resolved relative to the version
directory, not the parent package directory.

## Motivation

Today, `downloadURL` exists as metadata but does not drive install behavior.
That means a package directory must already contain the app payload before
`Install` can meaningfully create shortcuts, PATH entries, environment
variables, and wrapper scripts.

The desired behavior is:

1. A package can be represented by `pkg.toml` plus an origin declaration.
2. `Install` can bootstrap `App/` when it is missing or empty.
3. Same-version reinstall remains a repair operation for external state.
4. Re-downloading is explicit when `App/` is already populated.

## Non-goals

Do not turn the install flow into a generic plugin framework.

This feature is a narrow origin-population step. The rest of install should
remain the explicit fixed sequence already used by the project:

1. manage `current`
2. populate origin when needed
3. create shortcuts
4. write environment variables
5. manage PATH
6. create wrapper/bin files

Out of scope:

- persistent download cache
- package repository/index support
- non-zip built-in archive formats
- generic origin provider registries
- dependency resolution for package payloads or arbitrary configuration
- resumable downloads
- signature verification
- automatic execution of arbitrary remote installer formats

## Schema

### Built-in zip origin

```toml
[origin]
url = "https://example.com/tool-portable.zip"
checksum = "sha256:0123456789abcdef..."
extractSubdir = "tool-portable"
```

Fields:

- `url`: Required for built-in origin mode. Must be an HTTP or HTTPS URL.
- `checksum`: Optional checksum in `algorithm:hex` form. Initially support
  only `sha256:<hex>`.
- `extractSubdir`: Optional relative path inside the extracted archive whose
  contents should become the contents of `App/`.

If `extractSubdir` is omitted, `pkg` copies the contents of the archive root
into `App/`.

The blueprint intentionally does not use a `stripRoot` flag. `extractSubdir`
covers the important case more explicitly:

- archive contains `tool/tool.exe`
- user wants `App/tool.exe`
- set `extractSubdir = "tool"`

If an archive has exactly one top-level directory and the user wants that
directory stripped, they should declare that top-level directory with
`extractSubdir`. Avoid implicit root stripping because it makes archive layout
changes surprising and harder to debug.

### Script origin

```toml
[origin]
script = "scripts/populate-app.ps1"
```

Fields:

- `script`: Required for script origin mode. Path is relative to the pkg root,
  meaning the version directory containing `pkg.toml`.

`script` and `url` are mutually exclusive. A config that declares both is
invalid.

The script runs with its working directory set to the directory containing the
script. It receives enriched package metadata as JSON on stdin and is
responsible for populating `App/`.

Supported script executables:

- `.ps1`: run with PowerShell
- `.cmd`: run directly through Windows command execution
- `.bat`: run directly through Windows command execution
- `.exe`: run directly

Do not add extension-based support for `.py` in the first implementation. A
Python script can be wrapped by a `.cmd` or `.ps1` if needed. This keeps the
runtime contract smaller and avoids a second interpreter-selection policy.

## Migration from `downloadURL`

Move current top-level `downloadURL` into `[origin].url`.

Old:

```toml
downloadURL = "https://example.com/tool.zip"
```

New:

```toml
[origin]
url = "https://example.com/tool.zip"
```

Implementation guidance:

1. Update the canonical schema to remove top-level `downloadURL`.
2. Add `[origin]` as the canonical place for origin metadata.
3. Update `UpdateConfig` starter output to show `[origin]`.
4. Update helper scripts that emit `downloadURL` so they emit `[origin].url`.
5. Decide whether top-level `downloadURL` is rejected immediately or accepted
   only by migration helpers.

Preferred policy:

- `pkg Install` should reject top-level `downloadURL` as non-canonical once this
  feature lands.
- migration helper scripts may read old `downloadURL` and write `[origin].url`.

This matches the repo's current direction toward one canonical `pkg.toml`
schema.

## CLI

Add a new install option:

```text
--refresh-app
```

Behavior:

- If `App/` is missing, origin population runs.
- If `App/` exists and is empty, origin population runs.
- If `App/` exists and is non-empty, origin population is skipped by default.
- If `--refresh-app` is passed, `pkg` deletes the existing `App/` directory and
  repopulates it from origin.

Add a checksum override:

```text
--no-checksum
```

Behavior:

- If `[origin].checksum` is present, verify it by default.
- If verification fails, abort before mutating `App/`.
- If `--no-checksum` is passed, skip checksum verification and print a clear
  warning.

Do not overload existing `--force`.

`--force` already controls whether `current` may be replaced when a newer
version is active. Reusing it for destructive `App/` refresh would make install
semantics harder to audit.

## Origin population order

Run origin population after config loading/validation and after successful
`current` management, but before component installation.

The target install flow should be:

1. Resolve input path.
2. Load and validate `pkg.toml`.
3. Repair config metadata if `--fix-config` is used.
4. Enforce `only_portable` and admin policy.
5. Manage `current` unless installing from `current`.
6. Populate `App/` from origin when needed.
7. Run the existing component install sequence.

Reasoning:

- config must be validated before any download or script execution
- `only_portable` and Machine-scope admin checks should fail before network or
  script side effects
- `current` should point at the selected version before component paths are
  installed
- component installation should see a populated `App/`

If `current` management decides that a newer version is already installed and
component installation is skipped, origin population should also be skipped.

## App emptiness policy

`App/` is considered empty when:

- it does not exist, or
- it exists and contains no files or directories

`App/` is considered populated when it contains any entry, including hidden
files.

Default install must not delete or overwrite a populated `App/`. It should log
that origin population was skipped because `App/` is already populated.

`--refresh-app` is the only built-in path that clears a populated `App/`.

When clearing `App/`, delete only the resolved version directory's `App/`
subdirectory. The implementation must resolve the path first and verify it is
exactly under the current package version directory before recursive deletion.

## Built-in zip origin behavior

The built-in downloader should use only Python standard library functionality.

Recommended implementation shape:

1. Validate `[origin]` shape during config normalization.
2. When population is needed, create a temporary staging directory outside
   `App/`.
3. Download `[origin].url` to a temporary file.
4. If `[origin].checksum` is present and `--no-checksum` is not set, verify the
   downloaded file.
5. Extract the zip into staging.
6. Resolve the selected source directory:
   - staging root when `extractSubdir` is absent
   - staging joined with `extractSubdir` when present
7. Validate the selected source directory:
   - it exists
   - it is a directory
   - it resolves inside staging
8. Populate a temporary app directory next to `App/`.
9. Replace or create `App/` only after download, checksum, extraction, and
   source selection all succeed.
10. Remove temporary files/directories after success or failure.

Do not extract directly into `App/`.

This avoids leaving a half-populated application directory when download,
checksum verification, or extraction fails.

## Zip safety rules

Zip extraction must reject unsafe entries before writing files.

Reject any zip member that:

- is absolute
- contains `..` path traversal
- resolves outside the staging directory
- has a Windows drive prefix

If a zip contains an unsafe entry, fail the origin step and do not mutate
`App/`.

Symlink behavior can be conservative: if Python exposes an entry as a symlink or
the implementation cannot confidently preserve it safely, reject it. Windows
portable app archives usually do not require symlinks.

## Checksum policy

Supported syntax:

```toml
[origin]
checksum = "sha256:0123456789abcdef..."
```

Rules:

- checksum is optional
- if present, it is enforced by default
- `--no-checksum` skips enforcement and logs a warning
- initially support only `sha256`
- reject unsupported algorithms during config validation
- compare checksum case-insensitively
- require valid hex length for SHA-256

Do not infer checksums from URL sidecars in the first implementation.

## Script origin behavior

Script origin is the explicit escape hatch for packages whose payload cannot be
handled by simple zip extraction.

The script contract:

- path is relative to pkg root, meaning the resolved version directory
- working directory is the script's containing directory
- stdin receives enriched JSON
- stdout/stderr are streamed or captured into normal `pkg` logging
- exit code `0` means success
- any nonzero exit code fails install before component installation
- script must populate `App/`

After a successful script exit, `pkg` must verify that `App/` exists and is
non-empty unless the design later adds an explicit `allowEmptyApp` flag. Do not
add that flag in the first implementation.

Script paths must be validated:

- resolve relative to pkg root
- reject absolute paths
- reject parent traversal outside pkg root
- require the file to exist
- require one of the supported extensions

## JSON stdin contract

The script receives a JSON object.

The object should include the parsed canonical package configuration plus
resolved package variables.

Recommended shape:

```json
{
  "config": {
    "name": "Tool",
    "version": "1.2.3",
    "localVersion": 1,
    "only_portable": false,
    "origin": {
      "script": "scripts/populate-app.ps1"
    },
    "shortcut": [],
    "environment": [],
    "path": [],
    "bin": []
  },
  "identity": {
    "name": "Tool",
    "version": "1.2.3",
    "localVersion": 1,
    "versionString": "v1.2.3.l1"
  },
  "PkgVars": {
    "PkgRoot": "C:\\Tools\\Tool\\v1.2.3.l1",
    "App": "C:\\Tools\\Tool\\v1.2.3.l1\\App",
    "Icons": "C:\\Tools\\Tool\\v1.2.3.l1\\Icons",
    "Shortcuts": "C:\\Tools\\Tool\\v1.2.3.l1\\Shortcuts"
  }
}
```

Use `PkgVars` exactly as the top-level key name because that is the requested
script interface.

The paths in `PkgVars` should be absolute strings. The script should not need
to reconstruct package layout or know how `$App`, `$Icons`, and `$Shortcuts`
expand internally.

## Runtime config shape

Extend the normalized runtime config dict with an `origin` key.

Suggested normalized shapes:

No origin:

```python
"origin": None
```

Built-in zip origin:

```python
"origin": {
    "mode": "zip",
    "url": "https://example.com/tool.zip",
    "checksum": "sha256:...",
    "extractSubdir": "tool",
}
```

Script origin:

```python
"origin": {
    "mode": "script",
    "script": "scripts/populate-app.ps1",
}
```

Keep the shape simple. Do not add origin provider classes or a registry.

## User-visible logging

The origin step should log enough information to be useful without being noisy.

Examples:

- `App is missing; populating from origin...`
- `App is already populated; skipping origin population`
- `--refresh-app enabled; clearing App before origin population`
- `Downloading origin: https://example.com/tool.zip`
- `Verifying sha256 checksum...`
- `Checksum verification skipped because --no-checksum was provided`
- `Extracting zip archive...`
- `Using archive subdirectory: tool`
- `Running origin script: scripts/populate-app.ps1`

Error messages should name the failing field or path:

- `[origin].url must be an HTTP or HTTPS URL`
- `[origin].script cannot escape the package version directory`
- `[origin].extractSubdir was not found in the archive`
- `[origin].checksum did not match downloaded file`

## Failure behavior

Origin population failure should abort install before component installation.

If default population runs because `App/` is missing or empty and the origin
step fails:

- return a mutation/error result
- leave no partial `App/` directory if possible
- keep existing config untouched

If `--refresh-app` is used and the origin step fails:

- prefer staging-first replacement so the old `App/` is not deleted until the
  new payload is ready
- if implementation must remove old `App/` before script execution, document
  that script mode is destructive under `--refresh-app`

Preferred implementation for built-in zip mode:

- prepare new app contents in a sibling temp directory
- replace `App/` only after successful preparation

Script mode cannot be made equally atomic unless the script writes to a staging
path. Do not complicate the first contract with script staging. Instead:

- for missing/empty `App/`, run script directly
- for populated `App/` plus `--refresh-app`, clear `App/` and run script
- make this destructive behavior explicit in docs and logging

## Tests

Follow `docs/tests.md`: test observable behavior, not implementation layout.

Add tests around these public behaviors.

### Config normalization and validation

Protect:

- `[origin].url` is accepted
- `[origin].script` is accepted
- declaring both `url` and `script` fails
- unsupported checksum algorithm fails
- malformed SHA-256 checksum fails
- top-level `downloadURL` is no longer accepted by install config
- helper migration emits `[origin].url`

### Built-in zip population

Protect:

- install populates missing `App/` from a zip origin
- install populates empty `App/` from a zip origin
- install skips zip download when `App/` is already populated and
  `--refresh-app` is absent
- `--refresh-app` replaces existing `App/` contents
- `extractSubdir` copies only the selected directory contents into `App/`
- missing `extractSubdir` fails without mutating existing `App/`
- unsafe zip paths are rejected without mutating `App/`

Mock the network boundary. Do not perform live HTTP requests in tests.

### Checksum

Protect:

- checksum match allows install
- checksum mismatch aborts before `App/` mutation
- `--no-checksum` allows install and emits a warning when checksum is present

### Script origin

Protect:

- script receives enriched JSON on stdin
- `PkgVars.App` points to the resolved version directory's `App/`
- script working directory is the script's containing directory
- nonzero script exit fails install before component installation
- script path escaping pkg root is rejected
- unsupported script extension is rejected
- successful script must leave `App/` non-empty

Use local fixture scripts. Do not test PowerShell internals beyond the
observable process contract.

### Install sequencing

Protect:

- origin population runs before shortcuts/wrappers are created
- if a newer `current` is preserved and component install is skipped, origin
  population is skipped too
- same-version reinstall still repairs components and does not re-download when
  `App/` is already populated

## Documentation updates

Update `README.md`:

- describe `[origin]`
- show built-in zip example
- show script origin example
- document `--refresh-app`
- document checksum behavior and `--no-checksum`
- remove top-level `downloadURL` from canonical metadata docs

Update `docs/development_guide.md` if it describes config or install flow.

Update helper script README/examples so generated packages use:

```toml
[origin]
url = "..."
```

instead of top-level `downloadURL`.

## Implementation notes for an LLM developer

Keep the implementation inside `src/pkg/pkg.py`. This repo intentionally keeps main
runtime behavior in one Python file.

Suggested local functions:

- `normalize_origin_config(raw_origin)`
- `app_needs_origin_population(identity, refresh_app)`
- `populate_app_from_origin(identity, runtime_config, no_checksum, refresh_app)`
- `populate_app_from_zip_origin(identity, origin, no_checksum, refresh_app)`
- `populate_app_from_script_origin(identity, origin, runtime_config, refresh_app)`
- `safe_extract_zip(zip_path, destination)`
- `build_origin_script_payload(identity, runtime_config)`

These helpers should stay direct and concrete. Do not add base classes,
registries, provider objects, or plugin abstractions.

`install_package()` should remain the visible coordinator. Add the origin step
as an explicit block between junction management and component installation.

## Acceptance criteria

The feature is complete when:

1. `pkg.toml` supports canonical `[origin]` configuration.
2. Top-level `downloadURL` is no longer the canonical schema.
3. Missing or empty `App/` is populated from `[origin].url` zip by default.
4. `--refresh-app` explicitly replaces an already populated `App/`.
5. Zip extraction supports `extractSubdir`.
6. Zip extraction rejects unsafe archive paths.
7. `sha256` checksum is enforced when present.
8. `--no-checksum` skips checksum verification with a warning.
9. `[origin].script` runs relative to the pkg root and receives enriched JSON
   with `PkgVars`.
10. Script origin verifies that `App/` exists and is non-empty after success.
11. Existing component install semantics remain repair-oriented.
12. Tests cover observable behavior without live network access.
13. README and helper examples use `[origin].url`.

## Open decisions intentionally closed by this blueprint

- Built-in archive support starts with zip only.
- There is no persistent cache.
- `extractSubdir` is explicit; there is no automatic root stripping.
- `--refresh-app` is separate from `--force`.
- Checksums use `sha256:<hex>` syntax.
- Script origin supports `.ps1`, `.cmd`, `.bat`, and `.exe` initially.
- Script JSON includes a top-level `PkgVars` dictionary.
