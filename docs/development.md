# Development notes

This repository keeps the implementation in one file, `pkg.py`, but it does
not treat that as an excuse for hidden layers or framework-style scaffolding.
The goal is direct code with clear data flow.

## File organization

`pkg.py` is split into labeled sections:

1. `Shared models and pure helpers`
2. `Windows integration boundary`
3. `Package-management logic and CLI`
4. `Script entry point`

Keep code in the section where it belongs. A helper that mutates Windows state
belongs in the Windows boundary. A helper that decides *whether* to perform a
step belongs in the package-management section.

## Preferred shapes

Prefer plain top-level functions unless a type has real state to carry.

Good candidates for classes:

- dataclasses and enums
- small state holders such as `PackageMetadata`
- framework hooks such as argparse actions or logging handlers

Avoid classes that only group static methods. If a helper does not own state,
make it a function.

## Package state flow

Resolve input once and carry the result forward.

The intended flow is:

1. `resolve_input_path()` returns `ResolvedInput`
2. `PackageMetadata` stores that resolved input and derives `PackageIdentity`
3. `set_scope()` computes `scope_paths` once
4. `load_config()` loads and validates runtime config once for the action
5. install/update functions operate on that state directly

Do not resolve the same package path twice inside one action. Do not recompute
`scope_paths` in downstream install helpers.

## Adding or changing install steps

When adding a new install behavior:

1. keep parsing and normalization in the config helpers
2. keep direct OS mutation in the Windows boundary when the operation is
   Windows-specific
3. expose one top-level step function that returns `StepResult`
4. register that function in `INSTALL_STEPS` when it is part of the install
   pipeline
5. test the behavior by patching the function directly

A step should report what changed and accumulate warnings/errors without hiding
side effects behind unnecessary wrappers.

## Config rules

`pkg` supports one canonical `pkg.toml` schema. Do not add compatibility layers
for old aliases or deprecated config shapes unless there is a deliberate,
current product decision to support them.

If a migration helper needs to understand an older format, keep that logic in
`helper_scripts/`, not in the runtime path.

## Documentation rules

Keep the docs focused on current truth:

- `README.md` is user-facing
- `docs/development.md` is contributor-facing
- `helper_scripts/README.md` explains migration helpers

Do not document internal functions or file layout as if they are a stable
public API. Historical design notes and cleanup plans should live outside the
main docs surface.

## Testing rules

Prefer behavior tests over structure tests.

Good tests:

- config normalization and validation
- install/update behavior
- wrapper behavior
- helper-script output that the runtime accepts

Avoid tests that only enforce:

- exact class names
- section-marker strings
- file-count or module-layout snapshots

When possible, patch top-level functions at the mutation boundary so tests stay
about outcomes rather than internal scaffolding.

## Docstrings and comments

Use docstrings for public helpers and for non-trivial internal helpers. Keep
comments for local clarification, not for narrating a refactor history.

Prefer comments that explain *why* a behavior exists, especially around Windows
quirks, config validation edges, or migration-tool limitations.
