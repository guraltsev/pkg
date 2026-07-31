# Manual smoke checklist

This snapshot uses `pkg/pkg.py` as the stable executable and action facade.
Implementation domains live directly under `pkg/`, covering shared
utilities, Windows integration, package layout, configuration, metadata
editing, components, origins, and update staging.

Suggested manual checks on a Windows machine:

1. Run `pkg.cmd --help`.
2. Run `pkg.cmd --version`.
3. Run `pkg.cmd config update <version-dir>` against a package without
   `pkg.toml` and confirm a documented starter config with commented examples
   is created.
4. Run `pkg.cmd <version-dir>` against a package with shortcuts,
   environment variables, PATH entries, and wrapper files.
5. Confirm the package `current` junction points to the expected version.
6. Confirm shortcuts and environment changes land in the selected scope.
