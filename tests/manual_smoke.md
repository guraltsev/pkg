# Manual smoke checks for phases 0 through 7

This snapshot keeps the earlier fixture safety net and adds coverage for the
phase 6 and 7 behavior changes:

- round-trip `pkg.toml` metadata sync that preserves comments, unknown keys,
  table order, and existing layout
- starter `pkg.toml` creation only for explicit `UpdateConfig`
- narrow metadata sync that does not try to canonicalize or repair unrelated
  runtime entries
- safer variable expansion with separate general/script modes
- non-silent failures for unresolved variables in PATH entries, shortcuts, and
  environment values
- PowerShell-friendly wrapper handling that leaves plain `$VAR` script tokens
  alone unless they are package variables

## Fixture inventory

- `tests/fixtures/GoodApp`
  - valid config
  - comment-heavy metadata
  - unknown top-level key
  - one shortcut, one environment variable, one PATH entry, one wrapper
- `tests/fixtures/MismatchApp`
  - directory-derived metadata intentionally disagrees with `pkg.toml`
  - comments surround the stale metadata fields
- `tests/fixtures/PwshApp`
  - wrapper content keeps literal PowerShell variables like
    `$ErrorActionPreference`, `$PSScriptRoot`, and `$args`
- `tests/fixtures/NoConfigApp`
  - version layout exists without `pkg.toml`
- `tests/fixtures/BadPathApp`
  - `[[path]] value = "$MISSING_VAR"`

## Automated smoke

From the project root:

```bash
python -m unittest -v
```

The current test file covers:

- safe import on a non-Windows host
- truthful action results for invalid input and failing install steps
- no-write default loading when `pkg.toml` is missing during install
- existing-file `UpdateConfig` preserving comments and creating `pkg.toml.bak`
- starter `pkg.toml` creation for explicit `UpdateConfig`
- metadata sync on structurally valid but runtime-invalid config content
- script-mode expansion preserving PowerShell variables while expanding `${VAR}`
- unresolved-variable failures for PATH and shortcut steps
- apostrophe-safe PowerShell shortcut command generation

## CLI smoke

Run these from a shell in the extracted package directory:

```bash
python pkg.py --help
python pkg.py --version
```

These should not create files or install any Python dependencies.

## Phase 6 acceptance checks

### Existing-file `UpdateConfig` preserves structure

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\MismatchApp\v2.0.0.l3
$LASTEXITCODE
```

Expected after the run:

- `pkg.toml` has `name = "MismatchApp"`
- `version = "2.0.0"`
- `localVersion = 3`
- surrounding comments are still present
- unknown keys/tables are still present
- `pkg.toml.bak` exists

### Missing config creates a starter file only for `UpdateConfig`

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\NoConfigApp\v0.9.0.l1
$LASTEXITCODE
Test-Path .\tests\fixtures\NoConfigApp\v0.9.0.l1\pkg.toml
```

Expected: success, with a new top-level metadata-only `pkg.toml`.

### Structurally valid but runtime-invalid config still metadata-syncs

Create a temporary package whose `pkg.toml` has stale metadata and an unrelated
malformed runtime entry (for example a `[[shortcut]]` table missing `targetPath`),
then run:

```powershell
python .\pkg.py --action UpdateConfig <broken-version-dir>
$LASTEXITCODE
```

Expected: success, with only the owned metadata fields changed. The malformed
runtime entry should be left untouched for manual repair.

## Phase 7 acceptance checks

### PowerShell wrapper content keeps shell variables literal

Use `PwshApp` and inspect the generated wrapper content or a direct expansion
check. Confirm that all of these remain literal:

- `$ErrorActionPreference`
- `$PSScriptRoot`
- `$args`

And confirm that `${SystemRoot}` expands when present.

### Missing PATH variable fails instead of adding `.`

```powershell
python .\pkg.py .\tests\fixtures\BadPathApp\v1.0.0.l1
$LASTEXITCODE
```

Expected: non-zero, with a clear unresolved-variable error. No silent `.` PATH
entry should be added.

### Missing shortcut variable fails the shortcut step

Create a temporary package whose shortcut target uses an undefined variable and
run:

```powershell
python .\pkg.py <version-dir>
$LASTEXITCODE
```

Expected: non-zero, with the shortcut step reporting the unresolved variable.

### PowerShell shortcut backend survives apostrophes

Use a shortcut name or Start Menu path containing an apostrophe and force the
PowerShell backend. Expected: the generated command escapes apostrophes and the
shortcut step succeeds.
