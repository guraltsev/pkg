# `pkg` targeted cleanup and hardening blueprint

## Purpose

This handoff is for a narrow cleanup, not a rewrite.

The repo already matches the intended architecture in the important ways:

- one main implementation file: `pkg.py`
- labeled sections inside that file
- one canonical `pkg.toml` schema
- direct install pipeline with very little indirection
- no need for module splitting, plugin systems, or compatibility layers

Baseline at handoff time:

- `pytest -q` passes: `30 passed`
- the repo is already close to the desired final shape

The job is to fix the actual correctness bugs, remove one piece of unnecessary abstraction, and document/protect a couple of intentional behaviors so later cleanup does not break them.

## Non-negotiable constraints

Keep these constraints in mind for every change:

1. **Keep the main implementation in one file**.
   - Do not split `pkg.py`.
   - Keep the existing section markers.

2. **Keep the code direct**.
   - No generic abstractions.
   - No future-proofing layers.
   - No plugin points.
   - No config adapter ladders.

3. **Keep configuration simple and strict**.
   - Continue accepting one canonical schema.
   - Do not reintroduce alias normalization.

4. **Touch only the areas required for the agreed final state**.
   - Do not do unrelated cleanup.
   - Do not broaden scope into general refactors.

5. **Imports may stay close to use**.
   - Preserving the current single-file section layout is more important than putting all imports at the top.

## Accepted final state

This blueprint assumes the following decisions are already made:

1. **Fix the `UpdateConfig` metadata rewrite bug**.
2. **Fix the `--fix-config` install ordering bug for stale `only_portable` metadata**.
3. **Keep path-like `shortcut.name` and `bin.name` behavior**.
   - This is intentional package flexibility.
   - Do **not** remove it.
   - Do document it clearly.
   - Do warn at runtime when the final output path escapes the default root or is absolute.
4. **Remove `Reporter`**.
   - Prefer one globally configured logging path.
   - Do not replace `Reporter` with a more elaborate abstraction.
5. **Treat hidden `--python` as a protected bootstrap contract**.
   - Do **not** remove it.
   - Document it so later cleanup does not delete it by accident.

## Why these are the real sources, not just symptoms

### 1) `UpdateConfig` can corrupt valid TOML

Current source:

- `sync_config_metadata_text()` in `pkg.py:3007-3114`
- the current line matcher at `pkg.py:3041`

Current behavior:

- it parses metadata lines with a regex that treats `#` as the start of a comment everywhere
- that is wrong for TOML strings, where `#` inside quotes is ordinary data

Observed symptom already reproducible in the current repo:

```python
# package root: C#Tool
# input line:
name = "C#Tool-OLD"

# after UpdateConfig today:
name = "C#Tool"#Tool-OLD"
```

That is invalid TOML.

This is a core-cause problem because the code is trying to do line-level TOML surgery with a regex that is not quote-aware. Patching one special case would still leave the same class of bugs in place.

### 2) `--fix-config` repair runs too late for Machine-scope `only_portable`

Current source:

- `PackageManager.install()` in `pkg.py:3260-3403`
- effective portability is computed before config inconsistency repair at `pkg.py:3300-3309`
- metadata inconsistency repair happens later at `pkg.py:3311-3351`
- the mismatch detector already knows about stale `only_portable` at `pkg.py:2764-2772`

Observed symptom already reproducible in the current repo:

- package directory name does **not** end in `-portable`
- `pkg.toml` says `only_portable = true`
- run install with `scope=Machine` and `fix_config=True`
- install aborts with `only_portable packages cannot be installed system-wide`
- config is never repaired first

This is a core-cause problem because policy is being derived from stale owned metadata before the owned metadata repair step runs.

### 3) Path-like output names are intentional behavior, but the contract is under-documented

Current source:

- shortcut expansion and final path construction: `pkg.py:1278-1362`
- wrapper expansion and final path construction: `pkg.py:1987-2077`
- docs currently describe these as simple `name` fields:
  - `docs/configuration.md:46-55`
  - `docs/configuration.md:73-78`
- extended help is even stricter than the code for shortcut names:
  - `pkg.py:2248-2266`

This is **not** a bug to remove. The implementation clearly allows output-path-like names on purpose. The gap is that the docs and runtime messaging do not explain it.

### 4) `Reporter` is ceremony without a real boundary

Current source:

- `Reporter` class: `pkg.py:357-397`
- `PackageManager` stores one: `pkg.py:3217-3223`
- many parts of the code still call `print()` directly anyway, for example:
  - `pkg.py:1549-1567`
  - `pkg.py:1653-1657`
  - `pkg.py:1718-1746`
  - `pkg.py:1833-1860`
  - `pkg.py:2053-2075`
  - `pkg.py:3631-3643`
- `docs/api.md` currently presents `Reporter` as public API: `docs/api.md:14-18`

This is not a useful abstraction boundary. It is extra plumbing plus direct `print()` side effects.

### 5) Hidden `--python` is a bootstrap contract, not cleanup debris

Current source:

- `pkg.cmd` documents interpreter priority and forwards `--python`:
  - `pkg.cmd:7-11`
  - `pkg.cmd:29-30`
  - `pkg.cmd:61-89`
  - `pkg.cmd:146-155`
- `pkg.py` intentionally accepts hidden `--python`:
  - `pkg.py:3547-3550`
- current test name groups it with removed compatibility flags:
  - `tests/test_pkg_pure.py:554-563`
- an old closed plan still suggests it could be removed:
  - `docs/issues/_closed/issue005-pkg_part1_compatibility_legacy_alias_plan.md:435-446`

The code already proves it is a supported repo-level launcher contract. The risky part is that the surrounding docs/tests still make it look disposable.

## Scope of work

The intended work items are:

1. Add regression tests first.
2. Fix metadata rewrite safely.
3. Reorder install-time metadata repair.
4. Document and warn for path-like output names.
5. Remove `Reporter` and consolidate output.
6. Document and protect `--python`.
7. Run the full test suite and a couple of focused manual checks.

Do not broaden beyond that.

---

## Phase 1: Add regression tests first

Use the existing `unittest` style in `tests/test_pkg_pure.py`. Do not switch the test suite to a different style.

### 1A. Add a regression test for `#` inside a metadata string

Add a test that:

- creates a temp package root named `C#Tool`
- creates `v1.0.0.l1/pkg.toml` with stale metadata
- runs `PackageManager().update_config(version_dir)`
- asserts:
  - result is ok
  - updated `pkg.toml` parses with `tomllib.loads()`
  - `name = "C#Tool"` is present
  - the file is **not** corrupted into `"C#Tool"#...`

Suggested name:

- `test_update_config_preserves_hash_inside_quoted_metadata_string`

### 1B. Add a regression test for `--fix-config` with stale `only_portable`

Add a test that:

- creates a temp package root whose directory name does **not** end with `-portable`
- writes `only_portable = true` in `pkg.toml`
- constructs `PackageManager(scope=Scope.MACHINE, fix_config=True)`
- sets `PROGRAMDATA` and `SYSTEMDRIVE` in the environment because the tests run cross-platform
- patches:
  - `is_current_user_admin()` -> `True`
  - `JunctionManager.update_current_junction_if_needed()` -> returns `True`
  - `PackageManager._install_components()` -> returns `StepResult(ok=True, changed=False)`
- runs install and asserts:
  - install succeeds
  - config is rewritten to `only_portable = false`
  - output includes `Configuration updated successfully.`
  - output does **not** include `only_portable packages cannot be installed system-wide`

Suggested name:

- `test_install_with_fix_config_repairs_portable_flag_before_machine_scope_gate`

### 1C. Add tests for path-like output-name warnings without removing flexibility

Add tests that prove two things at once:

- the behavior is still allowed
- a warning is emitted

At minimum add one bin-wrapper test because it is easy to exercise without Windows-specific shortcut creation.

Suggested bin test:

- temp package with one `BinSpec(name="../outside.cmd", content="@echo off")`
- User scope env vars set
- run wrapper creation
- assert wrapper is created outside the default bin root
- assert warning output is present

Optional but useful shortcut-side test:

- call `prepare_shortcut_spec()` or `ShortcutInstaller._prepare_shortcut()` with a path-like `name`
- assert warning output is present

Suggested names:

- `test_bin_wrapper_name_outside_default_root_is_allowed_but_warned`
- `test_shortcut_name_outside_default_root_is_allowed_but_warned`

### 1D. Add tests that protect hidden `--python`

Split the current test intent into two separate ideas:

1. short help should still hide `--python`
2. parser/CLI should still accept `--python`

Suggested tests:

- keep a help-hiding test, but rename it so `--python` is no longer grouped mentally with removed flags
- add a parser acceptance test that proves `main([... "--python", ...])` parses instead of erroring

Suggested names:

- `test_help_hides_bootstrap_only_python_flag`
- `test_main_accepts_hidden_python_bootstrap_arg`
- `test_help_hides_removed_compatibility_flags` (now only for actually removed flags)

---

## Phase 2: Fix `sync_config_metadata_text()` safely

### Files to touch

- `pkg.py`
- `tests/test_pkg_pure.py`

### Do not do

- do not add a TOML AST dependency
- do not try to solve this with a bigger regex
- do not rewrite unrelated config code

### Required change

Keep the current high-level structure:

1. parse the whole file with `tomllib.loads()` first
2. validate canonical top-level keys
3. update only owned metadata fields
4. preserve surrounding comments/layout when possible

But replace the current metadata-line regex parsing with a small quote-aware scanner for top-level assignment lines.

### Recommended implementation shape

Add one small private helper near `sync_config_metadata_text()`.

Suggested responsibility of the helper:

- inspect one candidate top-level assignment line
- return:
  - indent
  - key
  - value text
  - comment text (only when `#` is outside quotes)
- understand single-line quoted strings well enough not to treat internal `#` as a comment
- preserve line endings outside the helper or alongside the helper, whichever keeps the code smaller

Important rules:

- this helper only needs to support the narrow line shape that `UpdateConfig` is willing to edit safely
- if a metadata line cannot be parsed safely, **raise `ConfigValidationError`** instead of guessing
- safety is more important than aggressively preserving weird hand-authored edge cases

### Postcondition safety check

After rendering the updated text, parse it again with `tomllib.loads()` before returning.

Reason:

- the root fix is the quote-aware scanner
- the final parse is a cheap guardrail that prevents `UpdateConfig` from ever writing invalid TOML if the scanner misses a case

This is acceptable because it is small, direct, and local to the exact bug surface.

### Acceptance criteria

- existing `UpdateConfig` preservation tests still pass
- new hash-in-name regression test passes
- updated files remain valid TOML after rewriting
- comments and unrelated keys still remain intact for normal canonical files

---

## Phase 3: Reorder install-time metadata repair

### Files to touch

- `pkg.py`
- `tests/test_pkg_pure.py`
- `docs/architecture.md` (execution flow description)

### Do not do

- do not special-case only `only_portable`
- do not patch around the symptom with a one-off conditional
- do not change unrelated install behavior

### Required change

In `PackageManager.install()`:

1. resolve path
2. create `PackageMetadata`
3. load config
4. collect load warnings
5. check metadata consistency
6. if `--fix-config` is enabled and inconsistencies exist:
   - run `update_config()`
   - reload the config from disk
   - replace `raw_config_data`
   - replace `metadata.runtime_config`
7. only **after that** compute `effective_only_portable`
8. only **after that** enforce Machine-scope portability policy
9. continue with admin check, junction management, and component install

### Why reload matters

Reloading after `update_config()` is not optional.

Without a reload, the rest of the install path still holds stale in-memory config state. The repair step must become the source of truth for the current install attempt.

### Recommended implementation detail

Keep the repair logic narrow:

- use the existing `check_metadata_consistency()`
- use the existing `PackageMetadata.update_config()`
- use the existing `PackageMetadata.load_config()` for the reload

This should be a small movement of code, not a redesign.

### Acceptance criteria

- stale `only_portable = true` can be repaired first under `--fix-config`
- the Machine-scope block no longer fires on stale metadata that will be repaired
- same-version repair installs still work as before
- when `fix_config` is **not** enabled, the current "abort rather than mutate config as a side effect" behavior is preserved

---

## Phase 4: Keep path-like output names, but document them and warn

### Files to touch

- `pkg.py`
- `README.md`
- `docs/configuration.md`
- `docs/architecture.md` or `docs/review.md` only if needed
- `tests/test_pkg_pure.py`

### Important: behavior to preserve

Do **not** add validation that blocks these forms.

Do **not** reinterpret these fields as leaf filenames only.

This flexibility is intentionally afforded to packages.

### Required documentation updates

Update all user/developer-facing descriptions so they match reality.

#### `docs/configuration.md`

For both `[[shortcut]]` and `[[bin]]`, explain that `name` may be:

- a simple output name, or
- a path-like output location after variable expansion

Document that:

- expansion happens before final placement
- absolute paths and parent traversal are allowed
- this lets packages place outputs outside the default shortcut/bin root
- this is powerful and intentional, but package authors should use it sparingly

#### `README.md`

Add a concise note under config behavior or variable rules describing the same behavior.

#### `pkg.py` extended help (`EXTENDED_HELP`)

Fix the current mismatch where shortcut `name` is described as a simple file name only.

### Required runtime warning behavior

Add a warning when the final shortcut/wrapper destination is outside the default root or is absolute.

This is a warning only.

Do not block creation.

### Recommended implementation shape

A tiny private helper is acceptable here because the exact same warning rule is needed in two places.

Keep it small and local.

Suggested responsibility:

- determine whether an expanded output name is:
  - absolute, or
  - escapes the default root via parent traversal

Keep the helper private and narrow. Do not turn this into a generic sandbox/path-policy abstraction.

### Cross-platform note for tests

Tests run on a non-Windows host, but the tool targets Windows.

So the warning rule should be based on simple string/path-shape checks that work cross-platform, for example:

- parent traversal (`..`)
- drive-letter roots (`C:\...` or `C:/...`)
- UNC roots (`\\server\share`)
- leading slash or backslash after expansion

The goal is not perfect path normalization. The goal is a truthful warning when package output placement is surprising.

### Suggested warning text

Keep it plain and explicit, for example:

- `WARNING: shortcut output resolves outside the default shortcut root; this is allowed but unusual: ...`
- `WARNING: bin output resolves outside the default bin root; this is allowed but unusual: ...`

### Acceptance criteria

- path-like output names still work
- warnings appear for escaping/absolute destinations
- docs no longer describe the feature incorrectly

---

## Phase 5: Remove `Reporter` and consolidate output

### Files to touch

- `pkg.py`
- `docs/api.md`
- `docs/architecture.md`
- `tests/test_pkg_pure.py`

### Required outcome

- delete `Reporter`
- stop passing reporter objects around
- stop mixing one object-based output path with separate direct `print()` calls
- keep user-visible output simple

### Recommended design

Use one globally configured logging path.

Keep it flat.

Do **not** add logger injection, adapters, per-class loggers, or configurable logging frameworks.

A good minimal shape is:

- one module-level logger
- one simple configuration path
- optional tiny helper functions for info/warn/error if that keeps call sites smaller than repeating formatting rules

### Very important implementation caveat

Many existing tests capture output with `contextlib.redirect_stdout(...)`.

A normal `logging.StreamHandler(sys.stdout)` created at import time will bind the original stdout stream and may **not** be captured by those tests.

If you use logging, preserve current capture semantics.

Preferred minimal solution:

- a tiny custom handler that writes to `sys.stdout` at emit time
- formatter kept simple (`%(message)s`)
- no propagation

If the logging setup starts becoming more complicated than the output code it replaces, simplify. The goal is less machinery than `Reporter`, not more.

### Signature simplifications to make

Remove `reporter` plumbing from these areas:

- `InstallStep` type alias at `pkg.py:1200`
- `PackageMetadata.update_config()`
- `ShortcutInstaller.install_shortcuts()`
- `EnvironmentVariableManager.install_environment_variables()`
- `PATHManager.add_to_path()`
- `PATHManager.ensure_bin_in_path()`
- `BinFileCreator.install_wrappers()`
- the install-step adapter functions
- `PackageManager.__init__()` field `self.reporter`
- `main()` local `reporter = Reporter()`

The result should be simpler function signatures, not replacement plumbing.

### Output behavior to preserve

Keep the current human-facing style:

- info lines remain plain
- warning lines still read like `WARNING: ...`
- error lines still read like `ERROR: ...`

### Docs/tests to update

- remove `Reporter` from `docs/api.md`
- update `docs/architecture.md` so it no longer mentions reporter utilities
- update tests that currently instantiate `module.Reporter()`

### Acceptance criteria

- no `Reporter` class remains
- no reporter parameters remain where they are no longer needed
- user-visible output still appears on stdout and is still easy to read
- existing output-capture tests still pass after updates

---

## Phase 6: Protect and document hidden `--python`

### Files to touch

- `pkg.py`
- `README.md`
- `docs/architecture.md`
- `tests/test_pkg_pure.py`
- optionally `docs/issues/_closed/issue005-pkg_part1_compatibility_legacy_alias_plan.md`

### Important: do not change the launcher contract

Do **not** remove hidden `--python` from `pkg.py`.

Do **not** change `pkg.cmd` interpreter-selection behavior unless that is the explicit main goal of a future refactor.

This work item is about protection and documentation, not behavior change.

### Required code comment

Add an inline comment above the hidden argparse entry in `pkg.py` explaining that:

- `pkg.cmd` forwards `--python`
- the hidden arg is intentionally accepted by `pkg.py`
- it exists as part of the repo-level bootstrap/install contract
- it should not be removed casually during cleanup

### Required docs

#### `README.md`

Add a short section such as `Bootstrap interpreter selection` documenting the priority order already implemented in `pkg.cmd`:

1. `--python <exe-or-command>`
2. `PKG_PYTHON`
3. `pkg.python`
4. fallback to `python` on `PATH`

#### `pkg.py` extended help

Keep `--python` hidden from normal `--help` if desired, but mention it in `--help-extended` because it matters operationally.

#### `docs/architecture.md`

Add a short note that `pkg.cmd` and hidden `pkg.py --python` together form the bootstrap path for systems where Python is not already discoverable in the usual way.

### Test cleanup

Rename/split the current help test so it no longer implies `--python` is a removed compatibility flag.

Also add a parser acceptance test so hidden support is protected by the suite.

### Optional but recommended doc cleanup

The closed issue doc that still suggests deleting `--python` is now misleading.

Add a short note there or update that section so future grep-based cleanup does not revive the wrong plan.

### Acceptance criteria

- `--python` remains hidden from normal short help
- `--python` still parses
- docs clearly describe why it exists
- repo docs no longer frame it as cleanup debris

---

## File-by-file change map

This is the intended touch set.

### `pkg.py`

Required changes:

- fix `sync_config_metadata_text()`
- move metadata repair earlier in `PackageManager.install()` and reload after repair
- add warning logic for path-like shortcut/bin names
- remove `Reporter`
- consolidate output path
- add protective comment above hidden `--python`
- update `EXTENDED_HELP`

Keep the current section layout.

Do not split the file.

### `tests/test_pkg_pure.py`

Add/update tests for:

- `#` in quoted metadata strings
- `--fix-config` ordering with stale `only_portable`
- path-like output-name warnings
- hidden `--python` acceptance and hiding semantics
- removal of `Reporter` from tests that currently construct it

### `README.md`

Update:

- path-like `shortcut.name` / `bin.name` behavior
- bootstrap interpreter selection (`--python`, `PKG_PYTHON`, `pkg.python`, PATH fallback)

### `docs/configuration.md`

Update:

- `[[shortcut]].name`
- `[[bin]].name`
- warning-level guidance for outputs outside default roots

### `docs/architecture.md`

Update:

- install execution flow so config repair occurs before metadata-derived policy checks
- remove reporter references
- add a short bootstrap-interpreter contract note if that is the clearest place

### `docs/api.md`

Update:

- remove `Reporter` from the public/shared API list
- keep the rest of the public API list accurate after signature simplification

### Optional: `docs/issues/_closed/issue005-pkg_part1_compatibility_legacy_alias_plan.md`

Add a small note near the old `--python` discussion so future cleanup does not treat that section as current guidance.

---

## Non-goals

These are out of scope for this task unless they become unavoidable while editing the exact touched lines.

1. Do not split `pkg.py` into modules.
2. Do not redesign the runtime config model.
3. Do not reintroduce legacy config aliases.
4. Do not change package-variable expansion semantics.
5. Do not remove path-like `shortcut.name` / `bin.name` behavior.
6. Do not remove hidden `--python`.
7. Do not do broad stylistic cleanup just because nearby code looks imperfect.
8. Do not expand this into a generic logging framework.
9. Do not change wrapper scripts unless documentation there is clearly wrong.
10. Do not spend time on unrelated flags or cleanup ideas unless they block the agreed work.

## Suggested implementation order

Do the work in this order to keep the changes easy to reason about:

1. Add the two main regression tests first:
   - hash-in-name metadata rewrite
   - stale `only_portable` with `--fix-config`
2. Fix `sync_config_metadata_text()`.
3. Fix `PackageManager.install()` ordering and reload.
4. Add path-like-name warnings and docs.
5. Remove `Reporter` and simplify output plumbing.
6. Update `--python` docs/comments/tests.
7. Run the full test suite.
8. Do the focused manual checks listed below.

Reason for this order:

- the two real correctness bugs are the highest-value changes
- logging/output cleanup is broader and should not obscure the bug fixes
- docs/protection work is easiest once behavior is settled

## Focused manual checks after the test suite passes

### Check 1: `UpdateConfig` no longer corrupts `#` in strings

Re-run the `C#Tool` scenario manually and confirm:

- the updated file still parses
- the `name` line is valid TOML
- comments and unrelated content remain intact

### Check 2: stale `only_portable` is repaired before Machine-scope portability gate

Re-run the stale-flag scenario manually with patched admin/component behavior or on a controlled test setup and confirm:

- config is repaired first
- the old portability error does not fire from stale data

### Check 3: path-like output names still work but now warn

Check both a nested path and an escaping path.

Expected result:

- nested path inside the default root still works without unnecessary noise
- escaping/absolute destination still works but emits a warning

### Check 4: bootstrap contract is still intact

Confirm all of these remain true:

- `pkg.py --help` does not show `--python`
- `pkg.py --help-extended` documents bootstrap interpreter selection
- `main([... "--python", ...])` parses
- `pkg.cmd` docs still match the actual priority order

## Definition of done

This task is done when all of the following are true:

1. The current repo bug where `UpdateConfig` corrupts `C#Tool`-style metadata is fixed.
2. The current repo bug where stale `only_portable` blocks Machine-scope install before `--fix-config` can repair it is fixed.
3. Path-like `shortcut.name` / `bin.name` behavior remains supported.
4. That flexibility is documented clearly and warned at runtime when it escapes the default roots.
5. `Reporter` is gone and output is consolidated without adding more complexity than it removes.
6. Hidden `--python` remains supported, hidden from short help, and clearly documented as an intentional bootstrap contract.
7. The repo still passes the full test suite.
8. No unrelated architecture churn was introduced.

## Final note for the implementer

Bias toward the smallest complete fix.

If you find yourself introducing a reusable framework, a compatibility layer, or a generic abstraction, you are probably moving away from the intended design.

This cleanup should make the code **more direct**, not more clever.
