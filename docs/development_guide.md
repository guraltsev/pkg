# Development notes

This repository keeps the implementation in one file, `pkg.py`. 
The intended style is 
- explicit control flow, 
- direct data passing, 
- only a small number of types that represent durable concepts or final results.

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

Prefer plain top-level functions unless a type clearly improves correctness or
readability.

Good candidates for classes:

- enums such as `Scope` and `Action`
- durable value/result carriers such as `PackageIdentity`, `StepResult`, and
  `ActionResult`
- narrow framework hooks such as argparse actions or logging handlers

Avoid short-lived schema wrappers or broad context objects that mostly forward
state from one helper to another. 
If a function only needs a few values, pass those values directly.

## Inline flow and local constants

Keep simple operations readable in one pass. When validation and action are used
by a single caller, keep them together instead of extracting a private helper
that makes the reader jump around.

Prefer:

- direct iteration over the values being processed
- immediate `ValueError` or `ConfigValidationError` messages at the invalid value
- small key lists or hint maps defined next to the loop or operation that uses them
- explicit local result aggregation when a sequence is fixed

Avoid:

- one-off row normalizers or per-entry action helpers
- module-level constants whose only consumer is one local loop
- conversion layers that only rename data before the next function unpacks it
- generic registries or step lists for fixed install behavior

## Package state flow

The install/update path should stay easy to read from top to bottom.

The intended flow is:

1. `resolve_input_path()` returns `PackageIdentity` plus a boolean telling the
   caller whether the input was resolved through `current`
2. `compute_scope_paths()` returns a small mapping with only the filesystem
   paths the install flow uses
3. `read_runtime_config()` loads `pkg.toml`, normalizes it into one canonical
   dict/list structure, and validates that structure
4. `install_package()` manages `current`, runs origin population when needed,
   and then runs the fixed component install sequence
5. `update_package_config()` coordinates metadata repair using
   those explicit values rather than a wrapper object

Do not resolve the same package path twice inside one action. 
Do not rebuild a second internal config schema once the canonical normalized dict/list config has been produced.

## Install flow

`install_components()` is intentionally explicit. The install order is fixed, so
it should remain visible in one local block of code instead of being hidden
behind a registry or generic pipeline abstraction.

When changing install behavior:

1. keep parsing and normalization in the config helpers
2. keep direct OS mutation in the Windows boundary when the operation is
   Windows-specific
3. keep origin population as an explicit `install_package()` step before
   component installation
4. edit `install_components()` directly when changing shortcut, environment,
   PATH, or wrapper behavior so the order remains obvious
5. test behavior by patching the concrete top-level helper that performs the
   step

Repetition is acceptable when it avoids indirection and does not contain behavioral decisions that could diverge. 

## Config rules

`pkg` supports one canonical `pkg.toml` schema. The runtime config should stay
close to that file shape:

- top-level scalar metadata stays as plain values
- `origin` stays as either `None` or one normalized table with a direct mode
- `shortcut`, `environment`, and `bin` stay as lists of normalized dicts
- `path` stays as a list of strings

Do not add compatibility layers for old aliases or deprecated config shapes.

If a migration helper needs to understand an older format, keep that logic in
`pkg.modules/`, not in the runtime path.

## Documentation rules

Keep the docs focused on current truth:

- `README.md` is user-facing
- `docs/development_guide.md` is contributor-facing
- `docs/docstring_schema.md` explains how to write useful docstrings and comments
- `docs/tests.md` explains test policy

Document coordinating functions and behavior boundaries more heavily than small
passive helpers. The docs should make it easier to preserve the direct style,
not to rebuild internal scaffolding.

## Testing rules

Prefer behavior tests over structure tests.

Good tests:

- CLI-visible install, update, help, and config behavior
- file artifacts users can inspect, such as created or updated `pkg.toml` files and wrapper scripts
- wrapper behavior that is observable by invoking the wrapper
- documented helper-script workflows whose output is accepted by the runtime

Avoid tests that only enforce:

- exact class names
- section-marker strings
- file-count or module-layout snapshots
- private helper boundaries or temporary refactor scaffolding
- source-text details when the same contract can be checked by running the tool

When possible, patch top-level functions at the mutation boundary so tests stay
about outcomes rather than internal scaffolding.

Temporary tests for a refactor belong in `tests/devel`, must be clearly marked
as temporary, and are excluded from the default pytest run.

## Docstrings and comments

Use docstrings for public helpers and for non-trivial internal helpers. Keep
comments for local clarification. Prefer concise Google-style sections when
parameters, return values, or raised exceptions need explanation, but do not
force boilerplate sections onto obvious helpers.

Never narrate refactor history.

Comments should explain *why* a behavior exists, especially around Windows
quirks, config validation edges, same-version reinstall behavior, or junction
safety rules.
