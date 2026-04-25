# Manual smoke checklist

This snapshot uses a single-file implementation in `pkg.py`, with the code
organized into explicit sections:

- `Shared models and pure helpers`
- `Windows integration boundary`
- `Package-management logic and CLI`
- `Script entry point`

Suggested manual checks on a Windows machine:

1. Run `python pkg.py --help`.
2. Run `python pkg.py --version`.
3. Run `python pkg.py UpdateConfig <version-dir>` against a package without
   `pkg.toml` and confirm a documented starter config with commented examples
   is created.
4. Run `python pkg.py Install <version-dir>` against a package with shortcuts,
   environment variables, PATH entries, and wrapper files.
5. Confirm the package `current` junction points to the expected version.
6. Confirm shortcuts and environment changes land in the selected scope.
