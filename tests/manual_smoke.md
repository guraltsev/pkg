# Manual smoke checks for phases 0 and 1

This repo snapshot adds lightweight fixtures and a minimal pure-import test so the
single-file refactor can proceed with a safety net.

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

## Pure import smoke

From the project root:

```bash
python -m unittest tests.test_pkg_pure
```

This verifies that importing `pkg.py` on a non-Windows host no longer crashes
immediately and that a couple of pure helpers remain callable.

## CLI smoke (Windows)

Run these from a Windows shell in the extracted package directory.

```powershell
python .\pkg.py --help
python .\pkg.py --version
```

## Fixture-oriented checks (Windows)

### GoodApp

```powershell
python .\pkg.py --action UpdateConfig .\tests\fixtures\GoodApp\v1.2.3.l1
python .\pkg.py .\tests\fixtures\GoodApp\v1.2.3.l1 --fix-config
```

### MismatchApp

```powershell
python .\pkg.py .\tests\fixtures\MismatchApp\v2.0.0.l3
python .\pkg.py .\tests\fixtures\MismatchApp\v2.0.0.l3 --fix-config
```

### PwshApp

Inspect the generated wrapper and confirm the PowerShell variables remain
literal in the source fixture before later expansion work begins.

### NoConfigApp

```powershell
python .\pkg.py .\tests\fixtures\NoConfigApp\v0.9.0.l1
```

### BadPathApp

Use this fixture later when Phase 7 lands. For Phases 0 and 1 it is just a
captured repro fixture.
