# Helper scripts

The scripts in this directory are best-effort migration helpers.

They are meant to help move older package directories toward the current
`pkg.toml` schema, but they are **not** part of the supported runtime surface.
Expect to review their output manually.

## `legacy_to_pkg_toml.py`

Builds a canonical `pkg.toml` from legacy JSON files in one package version
folder.

Typical inputs it looks for include:

- `opt_pkg.json`
- `environment*.json` or `env*.json`
- `shortcut*.json`
- `bin*.json`

The converter is intentionally best-effort:

- unknown fields are ignored
- malformed files become warnings when possible
- missing metadata may be inferred from the directory name
- legacy variable spellings are rewritten toward current package variables
- output is always the current top-level metadata plus `[[shortcut]]`,
  `[[environment]]`, `[[path]]`, and `[[bin]]`

It does **not** preserve every old config shape, and it does not define a
compatibility promise for the main tool.

Example:

```bat
python helper_scripts\legacy_to_pkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1 --output C:\Packages\Ripgrep\v14.1.0.l1\pkg.toml
```

Use `--dry-run` to print the generated TOML instead of writing it.

## `shortcuts_to_pkg_toml.py`

Imports real Windows `.lnk` files from a package version folder's `_shortcuts`
directory into that folder's existing `pkg.toml`.

The importer reads shortcut fields through Windows Script Host, converts paths
inside package-owned `App`, `Icons`, and `Shortcuts` directories back to `$App`,
`$Icons`, and `$Shortcuts`, and rewrites `[[shortcut]]` tables with matching
names. Other TOML sections are preserved.

Example:

```bat
python helper_scripts\shortcuts_to_pkg_toml.py --dir C:\Packages\Ripgrep\v14.1.0.l1
```

Use `--dry-run` to print the updated TOML instead of writing it.

## Review checklist

After running a helper script, check at least these items:

- the package metadata matches the directory name
- shortcut targets and working directories still point where you expect
- imported shortcut names match their intended Start Menu folders
- environment variables use the intended values
- PATH entries are still appropriate
- wrapper script content still makes sense for the target shell

When in doubt, treat the helper output as a starting point and edit the TOML by
hand.
