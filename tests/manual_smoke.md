# Manual smoke checks for phases 0 through 3

This snapshot keeps the phase 0/1 fixture safety net and adds coverage for the
phase 2 and 3 behavior changes:

- truthful action results and exit codes
- no `sys.exit()` outside `main()`
- lazy TOML and shortcut backends
- `UpdateConfig` preserving existing TOML structure when editing an existing file
- `Install` using defaults when `pkg.toml` is missing without creating a file

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
- phase 2 result propagation for invalid install input
- phase 2 non-zero exit behavior for invalid config and shortcut-step failure
- phase 2 removal of the constructor-level admin exit path
- phase 3 no-write default loading when `pkg.toml` is missing
- phase 3 existing-file `UpdateConfig` preserving comments and creating `pkg.toml.bak`

## CLI smoke (Windows)

Run these from a Windows shell in the extracted package directory.

```powershell
python .\pkg.py --help
python .\pkg.py --version
```

These should not create files or install any Python dependencies.

## Phase 2 acceptance checks (Windows)

### Invalid package path returns non-zero

```powershell
python .\pkg.py .\tests\fixtures\DoesNotExist
$LASTEXITCODE
```

Expected: non-zero, with an input/path error.

### Invalid config returns non-zero

Create a temporary package whose `pkg.toml` has a malformed shortcut entry, then run:

```powershell
python .\pkg.py <broken-version-dir>
$LASTEXITCODE
```

Expected: non-zero, with a config validation error.

### Shortcut creation failure returns non-zero

Use a package that is otherwise valid but force the shortcut backend to fail in a test build or temporary repro, then run:

```powershell
python .\pkg.py <version-dir>
$LASTEXITCODE
```

Expected: non-zero, with the shortcut step listed in the failure summary.

### `UpdateConfig` in Machine scope does not fail early for lack of admin

```powershell
python .\pkg.py --action UpdateConfig --scope Machine .\tests\fixtures\GoodApp\v1.2.3.l1
$LASTEXITCODE
```

Expected: `0` when the file is already in sync, or a normal update result if the
config was edited. It must not fail immediately just because `--scope Machine`
was selected.

## Phase 3 acceptance checks (Windows)

### Existing-file `UpdateConfig` requires `tomlkit` only when needed

In an environment without `tomlkit`, run:

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\GoodApp\v1.2.3.l1
$LASTEXITCODE
```

Expected: non-zero, with a message explaining that `tomlkit` is required to
preserve comments and formatting when updating an existing `pkg.toml`.

### Missing config during Install writes nothing

```powershell
python .\pkg.py .\tests\fixtures\NoConfigApp\v0.9.0.l1
$LASTEXITCODE
Test-Path .\tests\fixtures\NoConfigApp\v0.9.0.l1\pkg.toml
```

Expected: install uses defaults, returns success or normal install-step status,
and `pkg.toml` is still absent afterward.

### MismatchApp preservation check

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\MismatchApp\v2.0.0.l3
$LASTEXITCODE
```

Expected after the run:

- `pkg.toml` has `name = "MismatchApp"`
- `version = "2.0.0"`
- `localVersion = 3`
- surrounding comments are still present
- `pkg.toml.bak` exists

## Fixture-oriented checks that remain relevant later

### GoodApp

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\GoodApp\v1.2.3.l1
python .\pkg.py .\tests\fixtures\GoodApp\v1.2.3.l1 --fix-config
```

### PwshApp

Inspect the generated wrapper and confirm the PowerShell variables remain
literal in the source fixture before later expansion work begins.

### BadPathApp

Use this fixture later when Phase 7 lands. For phases 0 through 3 it is a
captured repro fixture.
