# Job 01: Manager configuration and scoped inventory foundation

Status: Closed — implemented by commit `8e65010`.

Depends on: none

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Implement the non-UI domain foundation for fixed-location manager mode. At the
end of this job, Python callers can load and validate `gupkg-config.toml`,
discover both configured package roots, and inspect deterministic scoped target
records with truthful local installation status.

Do not add manager CLI routing, Textual screens, or bulk upgrades in this job.

## Required preparation

Read and follow:

- `AGENTS.md`
- `docs/tests.md`
- `docs/docstring_schema.md`
- `docs/python_rules.md`
- the complete issue 011 design document

Inspect the existing public behavior in `collection.py`, `layout.py`,
`configuration.py`, `core.py`, and the collection tests before editing.

## Work

1. Add the focused manager domain described by issue 011, preferably in
   `src/gupkg/manager.py`.
2. Implement strict parsing of this schema:

   ```toml
   mode = "manager"
   schema_version = 1

   [packages]
   system = 'D:\Programs'
   user = '%USERPROFILE%\Programs'
   ```

3. Implement safe `%NAME%`, leading `~`, and config-directory-relative path
   expansion without executing a shell. Reject unresolved variables and
   unknown keys with actionable errors.
4. Reject equal or nested resolved roots. Represent a missing, non-directory,
   or unreadable configured root as an incomplete scope so a later UI can
   explain it.
5. Reuse `discover_collection` independently for both roots and attach the
   configured scope to every result. Do not duplicate collection traversal.
6. Add strict layout-level inspection of `current` that distinguishes
   installed, not installed, and broken. It must not use the existing
   sole-version fallback when determining installed state.
7. Produce deterministic managed targets with stable IDs such as
   `user:vscode` and `system:vscode`, installed version, greatest local
   manifest-backed version, health, and diagnostics.
8. Add target selection by full target ID or selector. Duplicate selectors
   across scopes must require explicit scope.

Keep the implementation direct. Do not add a database, manager cache, provider
registry, generic repository abstraction, or a new package format.

## Observable behavior to protect

Add permanent tests for:

- valid configuration and both configured roots;
- strict top-level and `[packages]` keys, mode, and schema version;
- case-insensitive `%USERPROFILE%` expansion on Windows;
- failure for unresolved variables;
- relative paths anchored to the manager file;
- equal and nested root rejection;
- incomplete status for missing or unreadable roots;
- correct User/System ownership and stable target IDs;
- duplicate selectors remaining distinct and requiring scope to select;
- valid, missing, and broken `current` states;
- a sole version without `current` remaining not installed;
- bootstrap-only packages remaining available; and
- semantic version ordering and deterministic target ordering.

Use real temporary directory layouts. Mock only genuine operating-system
boundaries that cannot be exercised portably. Every new test module and test
function needs the behavior-oriented docstring required by `docs/tests.md`.

## Completion criteria

- The manager domain can be imported without importing Textual.
- No package or root is mutated while loading configuration or inventory.
- Existing collection and package-path behavior remains unchanged.
- New focused tests pass.
- The full existing test suite passes.
- `git diff --check` reports no whitespace errors.
