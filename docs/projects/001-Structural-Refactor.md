Below is the blueprint I would hand to the implementer. It keeps the runtime as one main file, `pkg.py`, and treats extra files only as tests/docs/helpers.

The biggest design choice from your note is now locked in:

**`UpdateConfig` and `--fix-config` must preserve user-authored TOML, including comments, unknown keys, and general layout.**
That means the refactor must stop treating config updates as “serialize a normalized dict back to disk.” It must become “parse the existing TOML as a document, mutate only owned fields, and write it back atomically.”

---

# Refactor blueprint for `pkg`

## Non-negotiable rules for the whole refactor

1. `pkg.py` stays the single runtime file.
2. No `sys.exit()` outside `main()`.
3. No import-time side effects. Importing `pkg.py` must not create folders, modify `sys.path`, or install dependencies.
4. Any operation that changes system state must either be atomic or have a rollback path.
5. `UpdateConfig` and `--fix-config` must preserve comments, unknown keys, and existing TOML structure.
6. No phase gets merged half-finished. Each phase must end with:

   * a working CLI,
   * accurate exit codes,
   * updated help/docs for any changed behavior,
   * passing smoke checks.

## What “consistent state” means after every phase

After every merged phase:

* `pkg.py` runs normally from `pkg.cmd`.
* `pkg --help` and `pkg --version` work.
* `Install` and `UpdateConfig` still exist and behave coherently.
* No dead public CLI options are left exposed.
* No helper silently fails while the tool still reports overall success.

---

# Target end-state inside the single file

Reorganize `pkg.py` into comment-delimited sections like this:

```python
# =============================================================================
# User-facing documentation, version, and constants
# =============================================================================

# =============================================================================
# Imports and optional backend loaders
# =============================================================================

# =============================================================================
# Enums, dataclasses, and custom exceptions
# =============================================================================

# =============================================================================
# Pure helpers
#   - version parsing/comparison
#   - path classification
#   - atomic file helpers
#   - reporting/result helpers
# =============================================================================

# =============================================================================
# Package identity and scope paths
# =============================================================================

# =============================================================================
# Runtime config model and validation
# =============================================================================

# =============================================================================
# TOML document I/O and round-trip config updates
# =============================================================================

# =============================================================================
# Variable expansion
# =============================================================================

# =============================================================================
# Windows backends
#   - junctions
#   - shortcuts
#   - registry env/path
# =============================================================================

# =============================================================================
# Install steps
# =============================================================================

# =============================================================================
# Orchestration
# =============================================================================

# =============================================================================
# CLI
# =============================================================================
```

That keeps the script browseable without splitting it into modules.

---

# Recommended internal types

Add these early and use them consistently.

```python
@dataclass(frozen=True)
class ResolvedInput:
    raw_path: Path
    package_root: Path
    version_path: Path
    input_kind: str            # "version", "current", "package_root"
    installing_from_current: bool

@dataclass(frozen=True)
class PackageIdentity:
    name: str
    version: str
    local_version: int
    version_string: str
    package_root: Path
    version_path: Path
    is_current: bool
    only_portable_by_name: bool

@dataclass(frozen=True)
class ScopePaths:
    scope: Scope
    shortcut_root: Path
    bin_dir: Path
    env_root: Any              # winreg root handle
    env_subkey: str

@dataclass
class ShortcutSpec:
    name: str
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""

@dataclass
class EnvVarSpec:
    name: str
    value: str

@dataclass
class BinSpec:
    name: str
    content: str

@dataclass
class PackageConfig:
    description: str | None = None
    homepage: str | None = None
    download_url: str | None = None
    only_portable: bool = False
    environment: list[EnvVarSpec] = field(default_factory=list)
    shortcut: list[ShortcutSpec] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    bin: list[BinSpec] = field(default_factory=list)

@dataclass
class ExpansionResult:
    value: str
    unresolved: list[str] = field(default_factory=list)

@dataclass
class StepResult:
    ok: bool
    changed: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

@dataclass
class ActionResult:
    ok: bool
    changed: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0
```

Also add a tiny reporter:

```python
class Reporter:
    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
```

Do not overengineer it. It can just wrap `print()` for now.

---

# Config preservation policy

This is the key policy that should govern all config work.

## Runtime parsing policy

For `Install`, the code may parse TOML into a normalized runtime model. It may accept aliases and case-insensitive keys for compatibility.

That normalization is **runtime-only**.

It must not imply that the file will later be rewritten in canonical form.

## Write-back policy

For `UpdateConfig` and `--fix-config`:

* If `pkg.toml` exists, parse it as a round-trip TOML document.
* Mutate only the owned metadata fields:

  * `name`
  * `version`
  * `localVersion`
  * `only_portable`
* Preserve:

  * comments,
  * unknown top-level keys,
  * unknown keys inside known tables,
  * existing table order,
  * existing formatting as much as the backend allows.

## Layout policy

Support both of these existing layouts without auto-migrating them:

* top-level metadata keys
* legacy `[[main]]` with exactly one table

Do not automatically convert `[[main]]` to top-level keys. That would be a structural rewrite and works against preservation.

For newly created files, use the simpler future-facing layout:

* top-level metadata keys
* repeated `[[path]]`, `[[environment]]`, `[[shortcut]]`, `[[bin]]`

## Fix scope policy

`--fix-config` should become intentionally narrow:

* it fixes directory-derived metadata mismatches,
* it does **not** attempt full canonicalization,
* it does **not** rename arbitrary keys,
* it does **not** restructure tables,
* it does **not** try to “repair” malformed entry schemas.

If the file is structurally invalid, fail clearly and tell the user to edit the config manually.

That is the safest interpretation of preservation.

---

# Dependency policy

Stop auto-installing Python packages during startup.

## Shortcuts

* Prefer `pywin32` if already installed.
* Otherwise use the PowerShell shortcut backend.

## TOML

Use this order:

* For read-only parse:

  * prefer `tomllib` on Python 3.11+,
  * otherwise use `tomlkit` if installed,
  * optionally keep `toml` as temporary read-only fallback if you want compatibility during transition, but do not auto-install it.

* For round-trip update:

  * require `tomlkit`.

If `UpdateConfig` or `--fix-config` needs to preserve an existing file and `tomlkit` is unavailable, exit non-zero with a clear message.

---

# Exit-code policy

Make exit codes predictable.

Recommended mapping:

* `0` success, including “no changes needed”
* `2` user/config/input/dependency problem
* `3` system mutation failure
* `4` unexpected internal failure

`argparse` already uses `2` for CLI misuse, which fits.

---

# Phase plan

## Phase 0 — Baseline, fixtures, and test harness

### Goal

Create a safety net before moving code around.

### Work

Add lightweight supporting files outside the runtime script:

* `tests/manual_smoke.md`
* `tests/fixtures/` or a documented way to create them
* optionally `tests/test_pkg_pure.py` for pure logic once import safety exists

Create these fixture scenarios:

1. **GoodApp**

   * valid config
   * comments
   * unknown metadata key
   * one shortcut, one env var, one PATH entry, one wrapper

2. **MismatchApp**

   * directory name says one version/localVersion
   * config says another
   * includes comments around metadata

3. **PwshApp**

   * wrapper content includes PowerShell variables:

     * `$ErrorActionPreference`
     * `$PSScriptRoot`
     * `$args`

4. **NoConfigApp**

   * no `pkg.toml`

5. **BadPathApp**

   * `[[path]] value = "$MISSING_VAR"`

### Acceptance

No runtime changes yet. The fixture package layout is ready and documented.

---

## Phase 1 — Mechanical single-file cleanup and import safety

### Goal

Make the code browseable and testable without changing behavior much.

### Work

1. Add the section banners shown above.
2. Move schema alias maps and repeated literal key lists to module constants.
3. Move Windows-only imports behind guards.

   * `import winreg` should no longer happen unconditionally at import time.
   * either import it lazily where needed, or guard it with `if os.name == "nt"`.
4. Move pure helpers out of classes where appropriate:

   * version comparison
   * path normalization
   * path classification helpers
5. Remove obviously dead state like `component_paths` if it truly is unused.

### Specific design choices

* Keep public names stable where easy.
* Do **not** change CLI behavior in this phase except making import safer.
* `PackageMetadata` can stay for now, but add comments marking which responsibilities will be extracted later.

### Acceptance

* `pkg --help` still works on Windows.
* `pkg --version` still works.
* Importing the file for pure tests no longer crashes immediately on non-Windows.
* No user-visible behavior should intentionally change yet.

---

## Phase 2 — Truthful results, no hidden success, and action-scoped admin checks

### Goal

Make the tool tell the truth about whether it succeeded.

### Work

1. Add `StepResult` and `ActionResult`.
2. Refactor:

   * `PackageManager.install()`
   * `PackageManager.update_config()`
   * all mutation helpers
     so that failures propagate upward.
3. Remove `sys.exit()` from `PackageManager.__init__()`.
4. Move the admin check into the action path that actually needs it.

   * `Install` with `Machine` scope should check admin before system mutations.
   * `UpdateConfig` should not require admin just because scope is `Machine`.
5. Move pause behavior out of action methods and into `main()` so business logic returns a result first.
6. Distinguish these outcomes in messages:

   * success with changes,
   * success with no changes,
   * failed with errors.

### Required code changes

* `ShortcutInstaller.install_shortcuts()` must aggregate failures instead of ignoring return values.
* `EnvironmentVariableManager.install_environment_variables()` must aggregate failures.
* `PATHManager.ensure_bin_in_path()` and `PATHManager.add_to_path()` must affect the final action result.
* `BinFileCreator.install_wrappers()` must aggregate failures.
* `UpdateConfig` must return non-zero on failure.

### Acceptance

Run these checks:

* invalid package path returns non-zero
* invalid config returns non-zero
* shortcut creation failure returns non-zero
* `--action UpdateConfig --scope Machine` does not immediately fail for lack of admin
* success case returns `0`

At the end of this phase, the script may still be structurally messy, but it must report status accurately.

---

## Phase 3 — Remove import-time side effects and switch to lazy backends

### Goal

Make `pkg.py` safe to invoke for help/version and predictable to import.

### Work

Delete or retire these startup behaviors:

* dependency cache directory creation
* `sys.path` mutation
* auto-install of `pywin32`
* auto-install of `toml`

Replace with lazy backend loaders:

```python
def get_shortcut_backend() -> str: ...
def load_toml_reader() -> TomlReader: ...
def load_roundtrip_toml_backend(require: bool) -> RoundTripBackend | None: ...
```

### Required behavior

* `pkg --help` performs no filesystem writes.
* `pkg --version` performs no filesystem writes.
* starting the process does not install packages.
* if a required backend is missing, fail only when that specific action needs it.

### Dependency messages

Use clear messages like:

* “`tomlkit` is required to update an existing pkg.toml while preserving comments and formatting.”
* “Install it with `pip install tomlkit`, or run pkg under a Python environment where it is already available.”

### Acceptance

* `pkg --help` and `pkg --version` are side-effect free
* `Install` still works with plain TOML parse when possible
* `UpdateConfig` on an existing file fails clearly if `tomlkit` is missing

---

## Phase 4 — Split input resolution, package identity, and scope paths

### Goal

Stop mixing path resolution, package identity, config, and install scope in one mutable class.

### Work

Introduce:

* `ResolvedInput`
* `PackageIdentity`
* `ScopePaths`

### Implementation details

#### 1. Input classification must happen before `.resolve()`

This fixes the current fragility where a user-supplied `...\current` can be dereferenced too early.

Implement one helper:

```python
def resolve_input_path(raw_path: Path) -> ResolvedInput:
    ...
```

Rules:

* if `raw_path.name` matches `v<...>.l<...>`, treat as explicit version dir
* if `raw_path.name.lower() == "current"`, validate it as a junction and resolve its target
* otherwise treat as package root and look for `raw_path / "current"`

Do not call `.resolve()` on the user path before deciding which of those cases it is.

#### 2. Package identity should be immutable

`PackageIdentity.from_resolved_input(resolved: ResolvedInput)` should derive:

* package name
* upstream version
* local version as `int`
* `version_string`
* `is_current`
* portability implied by package name suffix

#### 3. Scope paths should be centralized

One helper should compute:

* Start Menu target
* bin directory
* registry root/subkey

This replaces duplicated logic in `set_scope()`, `ensure_bin_in_path()`, and `get_bin_dir()`.

#### 4. Environment path roots must fail clearly if missing

If `APPDATA` or `PROGRAMDATA` is missing, fail with a clear error instead of building a relative path from `""`.

### Migration strategy

Do not delete `PackageMetadata` immediately if that makes the phase too large. It can become a thin facade temporarily that delegates to the new objects. Remove it only after the new flow is fully adopted.

### Acceptance

These must all work:

* install from explicit version dir
* install from `current`
* install from package root

And they must resolve to the same effective `version_path` when appropriate.

---

## Phase 5 — Separate runtime config model and validation from document editing

### Goal

Create a clean runtime config model that is independent of how the file is laid out on disk.

### Work

Introduce:

* `PackageConfig`
* `ShortcutSpec`
* `EnvVarSpec`
* `BinSpec`

Move runtime parsing into dedicated functions:

```python
def read_runtime_config(identity: PackageIdentity, use_defaults: bool) -> PackageConfig: ...
def normalize_runtime_config(raw: Mapping[str, Any], identity: PackageIdentity) -> PackageConfig: ...
def validate_runtime_config(config: PackageConfig) -> None: ...
def check_metadata_consistency(identity: PackageIdentity, raw_or_config) -> list[str]: ...
```

### Important rules

1. Runtime normalization may still:

   * accept aliases,
   * accept case-insensitive keys,
   * normalize legacy shapes.

2. Runtime normalization must not imply file rewrite.

3. If `pkg.toml` is missing during `Install`:

   * use defaults,
   * do **not** create a file as a side effect.

4. `UpdateConfig` becomes the explicit place to create a starter config if one does not exist.

### What to retire

Retire or decompose these responsibilities currently inside `PackageMetadata`:

* `_canonicalize_config_dict`
* `_validate_config_dict`
* `_load_from_dict`
* `load_config`
* `update_config`

They should no longer live inside one giant metadata object.

### Acceptance

* install still works with valid configs
* alias-heavy configs still parse for runtime
* missing config does not silently write a file during install
* consistency check uses explicit presence checks, not truthiness

---

## Phase 6 — Round-trip config preservation and targeted metadata sync

### Goal

Implement the preservation-first config writer.

### Work

Add a round-trip TOML document layer using `tomlkit`.

Recommended functions:

```python
def load_config_document(path: Path): ...
def locate_metadata_container(doc): ...
def sync_document_metadata(doc, identity: PackageIdentity) -> bool: ...
def create_starter_config(identity: PackageIdentity) -> str: ...
def write_text_atomic(path: Path, text: str, backup: bool = False) -> None: ...
```

### Metadata-container rules

When loading an existing file:

1. If it has `[[main]]`:

   * require exactly one table
   * update keys inside that table
2. Else:

   * update top-level keys in the document
3. If neither exists:

   * insert top-level metadata keys near the top of the document, after leading comments and before repeated tables

### Sync rules

Only auto-sync these keys:

* `name`
* `version`
* `localVersion`
* `only_portable`

Do **not** overwrite:

* `description`
* `homepage`
* `downloadURL`

unless you later add an explicit command for editing those fields. They are user-authored metadata, not filesystem-derived facts.

### Preservation rules

* Never rebuild the document from a normalized dict.
* Never replace the whole top-level table or whole `main` table if you can mutate individual keys instead.
* Unknown keys and comments must survive.

### New-file behavior

If `pkg.toml` does not exist:

* `Install` uses defaults and writes nothing.
* `UpdateConfig` creates a new starter `pkg.toml`.
* the new starter file should use top-level metadata keys, not `[[main]]`.

### Atomic write rule

All config writes must go through an atomic write helper and create `pkg.toml.bak` when modifying an existing file.

Recommended flow:

1. render final text
2. write temp file in same directory
3. flush and fsync
4. copy existing file to `.bak` if present
5. `os.replace(temp, target)`

### Acceptance

With a config containing comments and unknown keys:

* `UpdateConfig` changes only the expected metadata lines
* comments are preserved
* unknown keys are preserved
* unknown table fields are preserved
* file order is unchanged except possibly insertion of missing metadata keys
* `pkg.toml.bak` is created on modification

---

## Phase 7 — Variable expansion redesign and safer step validation

### Goal

Fix dangerous and fragile expansion behavior without breaking useful package variables.

### Why this phase matters

The current `$VAR` expansion model is too broad.

It can:

* turn missing variables into empty strings
* accidentally add `.` to PATH
* corrupt script contents, especially PowerShell wrappers

The current help text itself contains a PowerShell wrapper example that would be broken by the current expander.

### Work

Introduce expansion modes:

```python
class ExpansionMode(Enum):
    GENERAL = "general"      # shortcuts, env values, path values
    SCRIPT = "script"        # wrapper content
```

And a new API:

```python
def expand_text(text: str, identity: PackageIdentity, mode: ExpansionMode) -> ExpansionResult: ...
```

### Expansion rules

#### Package variables

Supported everywhere:

* `$App`
* `$Icons`
* `$Shortcuts`

#### Environment variables

Use this policy:

* `${VAR}` is supported everywhere
* plain `$VAR` remains supported only in `GENERAL` mode for backward compatibility
* plain `$VAR` is **not** expanded in `SCRIPT` mode, except package variables above

This preserves PowerShell and other script languages much better.

### Escaping

Keep `$$` for literal dollar.

### Unresolved-variable policy

* unresolved `${VAR}` is an error
* unresolved plain `$VAR` in `GENERAL` mode should be treated as an error or at minimum a step failure
* in `SCRIPT` mode, plain `$NAME` that is not a package variable remains literal text

### PATH safety rule

For PATH entries:

* do not call `normpath()` until after verifying the expanded value is non-empty
* reject empty expansions
* reject unresolved variables
* never allow an empty expansion to become `"."`

### Shortcut hardening

Create one helper that prepares shortcut data before backend-specific writing:

```python
def prepare_shortcut_spec(spec: ShortcutSpec, identity, scope_paths) -> PreparedShortcut: ...
```

This helper should:

* expand values
* validate required fields
* build the final `.lnk` path
* ensure parent directories exist

Then the pywin32 and PowerShell writers should only do backend-specific work.

Also fix PowerShell escaping:

* escape `shortcut_path` itself
* add `-NoProfile -NonInteractive`

### Acceptance

These checks are required:

* a PowerShell wrapper keeps `$ErrorActionPreference`, `$PSScriptRoot`, and `$args` intact
* `${SystemRoot}` in wrapper content expands if requested
* a missing variable in `[[path]]` causes a failure, not a silent addition of `.`
* a missing variable in a shortcut target causes that step to fail
* shortcut path with an apostrophe does not break the PowerShell backend

---

## Phase 8 — Atomic mutable operations and final install-step pipeline

### Goal

Make system mutations safer and the install flow extensible.

### Work

## Part A — Atomic wrappers

Use atomic writes for wrapper files too:

```python
def write_bytes_atomic(path: Path, content: bytes) -> None: ...
```

Flow:

1. write temp file in same directory
2. flush and fsync
3. `os.replace(temp, target)`

That prevents truncated wrappers on failure.

## Part B — Atomic `current` junction switching

Replace the current remove-then-create pattern with swap-and-rollback.

Recommended algorithm:

1. validate target directory exists
2. create `current.__new__` junction pointing to target
3. verify `current.__new__` points to the expected target
4. if `current` exists:

   * rename `current` to `current.__old__`
5. rename `current.__new__` to `current`
6. delete `current.__old__`
7. on failure after step 4:

   * if `current` is missing and `current.__old__` exists, rename it back

Use unique temp suffixes if you want to avoid collisions.

Before merging this phase, the developer should verify rename semantics for junctions on a Windows scratch directory.

## Part C — Install pipeline

Introduce a context object:

```python
@dataclass
class InstallContext:
    identity: PackageIdentity
    config: PackageConfig
    scope_paths: ScopePaths
    reporter: Reporter
    force: bool
```

Define the install steps in one ordered list:

```python
INSTALL_STEPS = [
    install_shortcuts,
    install_environment_variables,
    ensure_bin_in_path,
    install_extra_path_entries,
    install_wrappers,
]
```

Each step returns `StepResult`.

### Failure policy

* Precondition failures abort early:

  * bad input
  * invalid config
  * failed junction update
* Component-step failures should be aggregated:

  * run remaining component steps
  * return non-zero at the end
  * print a summary of all step failures

That gives the user a full diagnosis while still being honest that the install was only partially successful.

## Part D — Windows backend cleanup

While touching these helpers:

* review and correct the `SendMessageTimeoutW` ctypes signature and return handling
* keep broadcast failures as warnings only

### Acceptance

* wrapper updates are atomic
* junction update does not leave `current` missing on a mid-operation failure
* component failures produce a non-zero exit code and a summary
* the install flow reads as a sequence of clear steps

---

## Phase 9 — CLI cleanup, docs refresh, and wrapper script polish

### Goal

Make the surface area match the actual product.

### Work

1. Remove or hide `Compress` from public CLI choices until implemented.
2. Remove `--python` from `pkg.py` help output.

   * interpreter selection belongs to `pkg.cmd`
   * keep compatibility if you must, but suppress it from help
3. Keep `pkg.cmd` as the authority for interpreter selection.
4. Update `install.cmd`, `install-machine.cmd`, and `update-config.cmd` to use `call pkg.cmd ...`.
5. Update the module docstring and extended help so they match actual behavior:

   * no import-time dependency installs
   * `Install` does not auto-create config
   * `UpdateConfig` preserves comments/unknown keys
   * `${VAR}` is the recommended env expansion syntax
   * plain `$VAR` inside script content is not pkg expansion
6. Add a short `README.md` with:

   * package layout
   * config examples
   * dependency note for `tomlkit`
   * action/exit-code summary

### Acceptance

* help text matches runtime behavior
* dead/incomplete public flags are gone
* wrapper batch files return to their own control flow after calling `pkg.cmd`

---

# Exact behavioral changes the developer should intentionally introduce

These are good changes and should be documented, not hidden.

1. `pkg --help` and `pkg --version` no longer create folders or install dependencies.
2. `Install` no longer auto-generates `pkg.toml` when it is missing.
3. `UpdateConfig` preserves comments and unknown keys instead of rewriting the file canonically.
4. `--fix-config` only syncs filesystem-derived metadata; it does not perform full schema normalization.
5. `${VAR}` becomes the preferred environment-expansion syntax.
6. Plain `$VAR` is no longer expanded inside wrapper script content.
7. `UpdateConfig` on an existing file may require `tomlkit`.

---

# What not to do

1. Do not split the runtime into a package of many files.
2. Do not keep auto-installing dependencies at import time.
3. Do not keep rewrite-from-scratch TOML updates for existing files.
4. Do not auto-migrate `[[main]]` to top-level metadata.
5. Do not let low-level helpers swallow errors while the top-level command still returns `0`.
6. Do not keep calling `.resolve()` on the user input path before classifying it.
7. Do not try to make `--fix-config` rewrite arbitrary malformed configs. Keep it narrow and safe.

---

# Suggested implementation order inside the codebase

If the developer wants the shortest risk path, use this exact order:

1. Phase 0
2. Phase 1
3. Phase 2
4. Phase 3
5. Phase 4
6. Phase 5
7. Phase 6
8. Phase 7
9. Phase 8
10. Phase 9

Do not start TOML preservation before the runtime config model is separated.
Do not start atomic junction swap before result propagation is fixed.
Do not start variable-expansion redesign before there is a test fixture for PowerShell wrapper content.

---

# Minimum smoke matrix for final acceptance

These should all be run on Windows before calling the refactor done.

## Basic CLI

* `pkg --help`
* `pkg --version`

## Install path variants

* install from version directory
* install from package root
* install from `current`

## Config preservation

* `UpdateConfig` on a comment-heavy config preserves comments and unknown keys
* `--fix-config` corrects stale `version/localVersion` without reformatting unrelated sections

## Missing config

* install with no `pkg.toml` succeeds with defaults and writes nothing
* `UpdateConfig` with no `pkg.toml` creates a starter file

## Variable expansion

* missing PATH variable does not produce `.`
* PowerShell wrapper content keeps literal PowerShell variables
* `${SystemRoot}` expands where expected

## Exit codes

* config validation failure returns non-zero
* backend failure returns non-zero
* no-op success returns `0`

## Wrapper scripts

* `install.cmd`
* `install-machine.cmd`
* `update-config.cmd`

must all return control correctly because they use `call pkg.cmd ...`

---

# The intended end-state in one sentence

A single-file Windows package tool whose runtime model is cleanly separated from its config document model, whose system mutations are safe and truthful, and whose config updates preserve the user’s authored TOML instead of rewriting it.

If you want, I can turn this into a developer-facing checklist document or an issue-by-issue implementation tracker.
