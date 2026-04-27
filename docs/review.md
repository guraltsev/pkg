# Code review summary

## Main simplification wins

- The runtime now accepts one canonical `pkg.toml` schema instead of carrying
  alias maps and old-shape config flattening.
- `PackageConfig` is the single runtime representation used by the install
  pipeline.
- `PackageMetadata` is reduced to package identity, scope state, and the typed
  runtime config.
- TOML loading uses one stdlib path, and shortcut creation uses one backend
  path.
- `UpdateConfig` now has one narrow responsibility: create a starter config when
  missing, or sync canonical top-level metadata in an existing canonical file.

## What was removed

- schema aliases and the old `[[main]]` wrapper
- dict-shadow compatibility fields on `PackageMetadata`
- metadata-only upgrade branches and legacy `pkg.json` cleanup behavior
- the legacy JSON conversion helper script
- optional backend ladders for TOML loading and shortcut creation
- the `WindowsPlatform` facade and deprecated CLI passthrough flags

## Resulting tradeoffs

The code is smaller and easier to reason about, but the project is now stricter
about what it accepts:

- old config spellings are rejected instead of normalized
- only the canonical top-level metadata keys are auto-synced
- callers should rely on typed runtime config (`PackageConfig`) rather than ad
  hoc dictionaries

Those tradeoffs are intentional. They remove contradictory normalization logic,
reduce duplicated maintenance surfaces, and keep the main package flow easy to
audit.
