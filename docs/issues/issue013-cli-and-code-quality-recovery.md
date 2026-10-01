# Issue 013: CLI and code-quality recovery plan

Date: 2026-09-30
Priority: Critical
Change type: Behavior stabilization, CLI redesign, source layout, and targeted refactoring

## Objective

Restore one discoverable CLI, one context-resolution path, shared CLI/TUI
operations, and one installable source/version layout. Refactor only the
measured domain-boundary hotspots needed to reach that state.

Current problems include conflicting manager-activation rules, broken help,
inconsistent scope syntax, mixed human/TOML output, and a source tree that does
not match `pyproject.toml`.

## Design decisions

Approved decisions are binding for this plan. Resolve the remaining decisions
before Phase 1. Any later change must also update tests, CLI help, README, the
operations guide, and TUI behavior.

### 1. Manager configuration discovery - approved

Manager configuration is loaded only beneath the `manager` command.

- `gupkg manager --config PATH <subcommand>` uses that exact file.

Without `--config`, use the first existing `gupkg-config.toml` in this order:

1. The directory containing the imported `gupkg.cli` module, resolved as
   `Path(gupkg.cli.__file__).resolve().parent`. The standalone builder places
   its default `gupkg-config.toml` in that same directory beside `cli.py`.
2. The directory named by `GUPKG_HOME`.
3. `%APPDATA%\gupkg`, the Windows roaming application-data directory.

- An unset `GUPKG_HOME` or `APPDATA` location is skipped.
- If a higher-priority candidate exists but is invalid, exit with code 2; do
  not fall through to a lower-priority file.
- If no candidate exists, exit with code 2 and list the searched locations.
- `--config` is owned by `manager` and appears before its subcommand.
- Do not inspect the current directory or search parent directories.
- A missing or invalid selected manager configuration exits with code 2.
- Accept only the current manager schema, `schema_version = 2`. Reject older
  manager schemas; do not convert or normalize them.
- Package commands never load manager configuration.

### 2. Canonical command grammar - approved

Require an explicit command. The package path remains optional only for a
command that can resolve the current directory as a package:

```text
gupkg [global-options] <command> [command-options]

gupkg [global-options] install [PATH] [--allow-downgrade] [--refresh-app]
      [--no-checksum] [--shim-linkage dynamic|static]
gupkg [global-options] update [PATH] [--check-only | --download-only]
      [--no-checksum] [--shim-linkage dynamic|static]
gupkg [global-options] config-check [PATH]
gupkg [global-options] config-fix [PATH] [--no-backup | --backup=false]
      [--import-shortcuts true|false] [--output PATH]
gupkg [global-options] tui [PATH]
gupkg [global-options] manager [manager-options] <subcommand>
```

Package behavior:

- `gupkg install` installs the package resolved from the current directory.
- `gupkg install PATH` installs the package at `PATH`.
- `gupkg update [PATH]` checks, downloads, and installs an update.
- `gupkg update [PATH] --check-only` checks without downloading or installing.
- `gupkg update [PATH] --download-only` checks and stages without installing.
- `--check-only` and `--download-only` are mutually exclusive.
- `--no-checksum` and `--shim-linkage` are accepted in every update mode.
  `--no-checksum` has an effect only when a payload is verified;
  `--shim-linkage` has an effect only when wrappers are installed. Ignore them
  when the selected mode does not reach that work.
- `config-check [PATH]` validates package configuration without changing it.
- `config-fix [PATH]` repairs or normalizes package configuration and reports
  the changes made.
- `config-fix` creates a timestamped backup before changing an existing file.
  `--no-backup` and `--backup=false` are equivalent opt-outs.
- Old package-metadata versions are converted only through an explicit
  `config-fix` invocation; ordinary commands do not convert them implicitly.
- `config-fix --output PATH` is valid only for old-metadata conversion.
  `--import-shortcuts` is valid only when repairing current metadata. Reject
  either option when it does not apply.
- `tui [PATH]` opens package operations for the selected package.
- Omitting `PATH` is allowed only when the current directory resolves as a
  package.

Use one argparse grammar and parse once. Simplify only the repetitive update
verbs: replace `upgrade check`, `upgrade download`, `upgrade install`, and
`upgrade full` with one `update` command and its two limiting flags.

Decision 4 defines the manager subcommand tree.

#### `config-fix` contract

Resolve package layout without requiring valid metadata. An explicit path may
name a version directory, package root, `current`, or legacy package directory.
When `PATH` is omitted, apply the same structural resolution to the current
directory. Ambiguous package roots fail without writing.

Select exactly one repair mode:

1. If canonical `pkg.toml` exists, synchronize only directory-owned metadata
   (`name`, `version`, `localVersion`, and `only_portable`) and optionally
   import shortcuts.
2. If legacy package metadata exists, use the existing
   `legacy_to_gupkg_toml.py` migration implementation to interpret it and
   produce current `pkg.toml`.
3. If neither metadata form exists but the directory is a valid version
   directory, create the documented starter `pkg.toml`.

Legacy conversion is best-effort within the formats recognized by the existing
migration code. Consume every recognized legacy source, tolerate missing
optional values, and retain its established directory-derived defaults. If the
converter cannot produce one unambiguous, valid current document, report exit
code 2 and leave every file unchanged. Do not add a second legacy parser.

Do not guess at malformed canonical TOML, unknown canonical keys, or ambiguous
layouts. Preserve comments, ordering, and unrelated canonical fields when
synchronizing an existing document.

Construct and validate the complete replacement before any side effect. If the
destination content is unchanged, report no change and create no backup. Before
replacing an existing destination, create a sibling timestamped backup named
`<name>.bak.<YYYYMMDDTHHMMSSffffffZ>` unless backup is disabled. If that name
already exists, append the next integer suffix. Abort before replacement if
backup creation fails. Write the destination atomically. Archive imported
shortcut files only after the replacement succeeds. A failed write must leave
the original destination intact.

### 3. Option ownership - approved

The root parser owns `--help`, `--version`, `--scope`, `--format`, `--pause`,
and `--allow-hook-dependency-install`. All other options belong to the command
that uses them. A global option may be accepted and ignored when it does not
apply to the selected command; for example, `--format` has no effect on either
TUI command.

| Option                | Owner                | Purpose                                                                                    |
| --------------------- | :------------------- | ------------------------------------------------------------------------------------------ |
| `PATH`                | Package command      | Package or version directory; may be omitted only when the current directory is a package. |
| `--check-only`        | `update`             | Stop after checking update availability.                                                   |
| `--download-only`     | `update`             | Stop after checking and staging the update.                                                |
| `--allow-downgrade`   | `install`            | Permit activation when a newer version is currently active.                                |
| `--refresh-app`       | `install`            | Replace populated application payload from the declared origin.                            |
| `--no-checksum`       | Install/update leaves | Bypass an applicable checksum with a warning.                                              |
| `--shim-linkage`      | Binary-install leaves | Select dynamic or static native wrappers.                                                  |
| `--import-shortcuts`  | `config-fix`         | Control shortcut import while repairing current metadata; defaults to true.                |
| `--scope`             | Global               | Select installation scope; manager commands limit it to configured roots.                  |
| `--allow-hook-dependency-install` | Global   | Allow trusted package hooks to install missing Python imports for this invocation.          |
| `--pause`             | Global               | Wait for a keypress after a non-TUI command completes.                                      |
| `--config PATH`       | Manager              | Override the fixed manager configuration search with one exact file.                       |
| `--max-depth N`       | Manager              | Bound discovery below configured roots; defaults to 8 and requires `N >= 1`.                |
| `--format human|toml` | Global               | Select output format; defaults to `human`.                                                  |
| `--no-backup`         | `config-fix`          | Disable the default timestamped backup.                                                    |
| `--backup=false`      | `config-fix`          | Alias for `--no-backup`.                                                                   |

Remove the `--manager` flag; manager behavior is selected by the `manager`
command.
Remove `--root`; manager configuration owns collection roots. Remove
`--use-defaults` and `--dry-run`. Keep `--shim-linkage` leaf-owned; it applies
to `install`, full `update`, `manager self repair`, and `manager self update`.

`--allow-hook-dependency-install` is an invocation-wide, non-persistent security
policy. It defaults to false and permits trusted `pkg.local` hooks to install
and retry missing Python imports during update checks, update downloads,
manager updates, and bootstrap promotion during install. It does not control
installation of gupkg's own optional runtime dependencies. Ignore it when the
selected workflow cannot execute a package-local hook.

`--pause` applies to non-TUI CLI commands. Write its prompt to stderr so TOML
stdout remains parseable. Ignore it for TUI commands.

Do not retain aliases for the old command grammar, scope spellings, entry
point, or argument placement. The only compatibility feature is the explicit
old-to-new package-metadata conversion workflow.

### 4. Manager subcommand surface - approved

Group manager workflows beneath `manager`, with a specific subcommand for each
operation. `gupkg manager` without a subcommand prints manager help and does not
open the TUI implicitly; it exits with status 2 because the required subcommand
is missing.

```text
gupkg [global-options] manager [--config PATH] [--max-depth N] tui
gupkg [global-options] manager [--config PATH] [--max-depth N] list [--filter FILTER]
gupkg [global-options] manager [--config PATH] [--max-depth N] doctor
gupkg [global-options] manager [--config PATH] [--max-depth N] update
      [--check-only | --download-only] [--yes]
      [--no-checksum] [--shim-linkage dynamic|static]
gupkg [global-options] manager [--config PATH] [--max-depth N] install SELECTOR [--offline]
gupkg [global-options] manager [--config PATH] registry sync
gupkg [global-options] manager [--config PATH] registry status
gupkg [global-options] manager [--config PATH] search [QUERY] [--offline]
gupkg [global-options] manager [--config PATH] self status
gupkg [global-options] manager [--config PATH] self repair [--shim-linkage dynamic|static]
gupkg [global-options] manager [--config PATH] self update [--shim-linkage dynamic|static]
```

Manager `update` owns `--check-only`, `--download-only`, `--yes`,
`--no-checksum`, and `--shim-linkage`. `list` owns `--filter`; `install` and
`search` own `--offline`; `self repair` and `self update` own
`--shim-linkage`.

`--max-depth` defaults to 8 and accepts only positive integers. `list --filter`
accepts `all`, `installed`, `uninstalled`, `updatable`, or `unhealthy` and
defaults to `all`. `manager search` without `QUERY` lists every entry in the
active registry cache.

`manager install SELECTOR` resolves a case-insensitive exact selector only from
the configured registry cache; it does not select from installed inventory.
Reject path-looking, missing, duplicate, or invalid selectors. Online mode
synchronizes the registry only when no active cached tree exists. `--offline`
never contacts the network and requires a usable cached tree. Stage the
validated registry seed under the explicitly selected user or system root,
then delegate to the ordinary package installation operation.

Global `--scope` and `--format` are accepted consistently. Bulk update remains
confirmed and does not install uninstalled packages. `--check-only` and
`--download-only` never prompt and do not require `--yes`; ignore `--yes` in
those modes. A full update requires interactive confirmation unless `--yes` is
present. Batch work always continues after an individual target failure and
reports every target outcome; there is no fail-fast mode.

### 5. Public scope vocabulary - approved

Expose only `auto`, `user`, and `system`. Keep `Scope.MACHINE` internally.
Do not accept `all`, `Auto`, `User`, or `Machine`.

`auto` is the default. Package commands retain automatic scope selection.
Manager aggregate commands interpret `auto` as both configured roots and
interpret `user` or `system` as a root filter. Manager operations on one target
use the scope owned by that target. `manager install` requires an explicit
`--scope user` or `--scope system` because a new selector has no owned scope.

### 6. Invocation resolution - approved

Resolve the invocation once:

1. Help and version are context-free.
2. `manager` selects manager mode; its required subcommand selects the workflow.
3. `install`, `update`, `config-check`, `config-fix`, and `tui` select package
   mode.
4. Resolve the command's explicit `PATH`, or resolve the current directory
   when omitted.
5. A missing, ambiguous, or invalid package path is a user error.

Manager selection, registry selectors, and collection traversal occur only
beneath `manager`. They cannot affect package commands.

### 7. Output and exit-status contract - approved

`--format` defaults to `human`. In human mode, normal results go to stdout and
diagnostics go to stderr.

`--format toml` emits exactly one parseable TOML document to stdout after a
command has parsed, including expected command failures. Do not emit banners,
progress logs, prompts, or nested operation output to stdout. Put expected
warnings and errors in the document. Reserve stderr for argparse errors and
failures that occur before a result document can be constructed. TUI commands
ignore `--format` and retain normal interactive behavior.

Every TOML result starts with this envelope:

```toml
output_schema = 1
command = "update"
ok = true
changed = false
status = "current"
exit_code = 0
warnings = []
errors = []
```

Requirements:

- `command` uses the canonical dotted name, such as `config-fix`,
  `manager.update`, or `manager.registry.sync`.
- `exit_code` exactly matches the process exit status.
- `status` is a concise command-specific state; callers determine success from
  `ok` and `exit_code`, not by parsing `status` text.
- Omit inapplicable optional fields instead of emitting sentinel empty strings.
- Render paths as normalized absolute strings and order collections
  deterministically.
- Do not include credentials, authorization headers, or other secrets.

Add command-specific data after the envelope:

- Package install/update: `[package]` with path, identity, scope, installed
  version, and candidate version when known.
- Configuration commands: `[config]` with path, operation, and backup path when
  one was created.
- Manager inventory/update/install: `[manager]`, zero or more `[[target]]`
  records, and `[summary]` counts. Each target includes its own status,
  `changed`, exit code, warnings, and errors.
- Registry search/status: `[registry]` and zero or more
  `[[registry.package]]` records.
- Self status/repair/update: `[self]` with runtime and shim results.

Use these process exit codes:

- `0`: the command completed successfully, including current/no-change cases.
- `2`: invalid syntax, selection, scope, configuration, metadata, confirmation,
  or other user-correctable input.
- `3`: an expected operational failure such as network, download, filesystem
  mutation, lock, elevation, hook, dependency installation, or subprocess
  failure.
- `4`: an unexpected internal failure.

Argparse syntax errors use stderr and exit 2 without a TOML document. For batch
results, choose the most severe completed result in the order 4, 3, 2, 0 while
still reporting every target.

### 8. Source and version ownership

**Proposed decision:** move the runtime package to `src/gupkg`, retain
versioned directories only in standalone artifacts/fixtures, and establish one
release version source at `src/gupkg/_version.py`. Runtime code imports that
module; setuptools reads it dynamically; standalone assembly imports or reads
the same value.

**Decision:** Accepted.

### 9. Module boundaries

**Proposed decision:**

- `cli.py`: grammar, context resolution, dispatch, rendering, exit translation.
- `gupkg.py`: public single-package workflows with no CLI compatibility
  entrypoint.
- `manager.py`: public operations shared by CLI/TUI.
- `configuration.py`: top-level normalization coordinated from helpers named
  for documented TOML concepts.

Do not add a command framework, service container, schema framework, or
passive configuration object tree.

**Decision:** Accepted. Remove compatibility code except the explicit
old-to-new package-metadata conversion operation.

## Target behavior

- `gupkg --help` lists the full supported command surface.
- Package-command and manager-subcommand help succeeds without filesystem,
  configuration,
  registry, or network access.
- Syntax is parsed once; context is resolved once.
- `gupkg install PATH` behaves identically from every working directory.
- Manager and package selection use the same scope terms.
- CLI and TUI call the same public manager operations.
- Domain operations return result data; renderers own human/TOML output.
- Tests, editable installs, wheels, console scripts, module execution, and the
  standalone builder consume the same source and version.

## Delivery plan

Keep each phase independently reviewable. Do not combine this work with new
issue 012 product features.

### Phase 0: establish the baseline

Protected behavior: observable package, manager, update, and configuration
behavior that remains part of the approved CLI.

- [ ] Run the current focused and full suites without changing the parser.
- [ ] Classify every failure as regression, unfinished issue
  012 behavior, manual/platform-only, or stale expectation.
- [ ] Identify tests tied to implicit manager activation or the superseded CLI;
  replace them only during Phase 2.
- [ ] Document the CLI/context contract in `docs/operations.md`.

Exit:

- The pre-migration baseline and every known failure are documented.
- Retained observable behavior is separated from superseded parser behavior.

### Phase 1: repair packaging and version ownership

Protected behavior: installed and checkout entry points run identical code and
report one version.

- [ ] Move `src/v0.1/gupkg` to `src/gupkg`.
- [ ] Update build inputs, launchers, imports, and documentation paths.
- [ ] Remove `tests/runtime_paths.py` and use normal imports.
- [ ] Establish the approved version source and align standalone metadata.
- [ ] Point the console script and `python -m gupkg` directly at `cli.main`;
  remove legacy entrypoints rather than forwarding them.
- [ ] Correct package-data paths, including the shim README.
- [ ] Place the standalone default `gupkg-config.toml` beside the built
  `gupkg/cli.py` module.
- [ ] Build and inspect sdist/wheel; install the wheel in a clean environment.

Exit:

- Setuptools discovers one `gupkg` package.
- Console and `python -m gupkg` work outside the checkout.
- Runtime, package metadata, and standalone manifest versions agree.
- Required Python, shim, DLL, license, and README files are packaged.

### Phase 2: replace the parser

Protected behavior: documented commands, defaults, options, help, and syntax
errors.

- [ ] Build the package parser and its `manager` subparser in `cli.py`.
- [ ] Assign every option to one parser.
- [ ] Define `--scope`, `--format`, `--pause`, and
  `--allow-hook-dependency-install` once on the root parser.
- [ ] Accept only the approved lowercase scope spellings.
- [ ] Remove early help routing, nested parsing, manual token dispatch, and
  reconstructed argument lists.
- [ ] Temporarily delegate package commands and approved manager subcommands to
  existing domain helpers.
- [ ] Implement and test `install`, `update`, `config-check`, `config-fix`,
  `tui`, and the approved manager subcommands.
- [ ] Remove the old CLI grammar without aliases or fallback parsing.

Exit:

- Package-command and manager-subcommand help is context-free and complete.
- Invalid combinations fail at the owning parser.
- Each invocation calls `parse_args()` once.
- The old CLI grammar is rejected as invalid syntax.

### Phase 3: centralize context and dispatch

Protected behavior: explicit command selection and package path resolution.

- [ ] Implement the approved invocation resolution in one resolver.
- [ ] Return a small resolved-context value containing downstream facts only.
- [ ] Replace duplicate path/positional heuristics with one classifier.
- [ ] Add consistent unsupported-context diagnostics.
- [ ] Keep registry and collection resolution inside manager handlers.
- [ ] Reduce `main()` to parse, resolve, dispatch, render, and exit translation.

Exit:

- Context is resolved once.
- No handler reparses or recursively invokes the CLI.
- Subprocess tests cover `install PATH`, cwd-backed `install`, missing commands,
  and representative `manager` subcommands.

### Phase 4: establish shared manager operations

Protected behavior: CLI/TUI inventory, diagnostics, planning, revalidation,
update, and registry-install results.

- [ ] Move shared manager orchestration behind a public domain API.
- [ ] Return structured reports instead of printing or redirecting stdout.
- [ ] Route CLI rendering and TUI workers through the public API.
- [ ] Remove TUI imports of CLI-private helpers.
- [ ] Define the manager TOML schema in one renderer.
- [ ] Replace private update-helper imports with a narrow public update API.

Exit:

- CLI and TUI share plan, execution, and revalidation behavior.
- Manager operations do not print user output.
- Partial failures still produce one parseable TOML document.

### Phase 5: split configuration normalization by schema concept

Protected behavior: accepted `pkg.toml`, defaults, unknown-key rejection,
legacy guidance, path/checksum safety, and useful diagnostics.

- [ ] Extract origin/history normalization.
- [ ] Extract update check, payload, and step normalization.
- [ ] Extract component normalization for environment, shortcut, path, and bin
  rows where their semantics differ.
- [ ] Deduplicate checksum/safe-path validation only when field-specific errors
  remain intact.
- [ ] Keep cross-field rules in the coordinator.
- [ ] Apply `docs/python_rules.md` and `docs/docstring_schema.md` to changed code.

Exit:

- Each helper owns one documented schema concept.
- Valid normalized output is unchanged.
- Invalid inputs retain field-specific errors.
- No generic schema framework is introduced.

### Phase 6: review remaining hotspots

Protected behavior: staging atomicity, installation safety, wrapper behavior,
and provider selection.

Review in order:

1. `github_releases.check_update()`
2. `updates._prepare_update()`
3. `manager.load_manager_config()`
4. `components.install_wrappers()`
5. `install_package()`

For each function:

- [ ] Identify whether it is one sequential workflow or multiple concepts.
- [ ] Add narrated blocks for validation, side effects, safety, and cleanup.
- [ ] Extract only repeated validation, isolated side effects, durable concepts,
  or independently testable parsing.
- [ ] Preserve visible failure and cleanup ordering.

Lower branch count is not an exit criterion.

### Phase 7: align documentation and remove migration scaffolding

- [ ] Make `docs/operations.md` the canonical CLI/mode contract.
- [ ] Reduce README CLI content to an overview and common examples.
- [ ] Update `docs/development_guide.md` for actual paths and boundaries.
- [ ] Add concise supersession notes to affected issues 010-012.

Exit:

- Help, README, operations guide, development guide, and tests describe one
  command and context model.
- No active documentation references `src/v0.1/gupkg`.

## Test plan

Follow `docs/tests.md`. Test observable behavior; do not test helper names,
module placement, decision counts, or private call graphs.

### Permanent coverage

- Package-command/manager-subcommand help, unknown commands, invalid options,
  and version agreement.
- `gupkg manager` prints help and exits 2; `--max-depth` defaults to 8 and
  rejects non-positive values.
- Manager list filters have the approved choices/default, and an empty manager
  search lists all cached entries.
- `gupkg install PATH` and `gupkg install` from a package directory.
- Full `gupkg update`, `update --check-only`, and
  `update --download-only` behavior.
- Update modes accept `--no-checksum` and `--shim-linkage`, ignoring either
  option when the selected mode does not perform the relevant work.
- `config-check` is non-mutating; `config-fix` reports and applies its repairs.
- `config-fix` creates a timestamped backup before mutation by default;
  `--no-backup` and `--backup=false` suppress it.
- Explicit `config-fix` converts supported old package metadata to the current
  version; other commands do not perform compatibility conversion.
- Legacy conversion reuses the existing migration implementation and recovers
  every unambiguous recognized field without adding another parser.
- `config-fix` resolves package layout independently of metadata validity,
  preserves unrelated canonical text, rejects unsafe repairs without writing,
  and leaves the original intact after backup or replacement failure.
- Package `tui [PATH]` selects the requested package.
- Bare `gupkg` and cwd-backed install outside a package fail clearly.
- `--check-only` and `--download-only` are mutually exclusive.
- Explicit `manager` activation with default and explicit configuration paths.
- Default manager configuration precedence: Python-file directory,
  `GUPKG_HOME`, then `%APPDATA%\gupkg`.
- The first manager configuration candidate is beside the resolved
  `gupkg.cli` module in installed and standalone layouts.
- Invalid higher-priority manager configuration fails without fallback.
- Manager schema versions older than 2 are rejected without conversion.
- Manager configuration is not discovered from the current directory or its
  parents.
- Proof that manager configuration does not affect package commands.
- Missing/invalid manager config, ambiguous manager selector, and missing
  manager selector failures.
- `manager install` resolves exact registry selectors, honors offline cache
  isolation, rejects path-like selectors, and installs into the explicit
  scope.
- Manager check/download modes never confirm; full manager update confirms or
  requires `--yes`, continues after individual failures, and reports all
  outcomes.
- Canonical scope values, rejection of `all` and removed spellings, aggregate
  `auto` behavior, and explicit scope enforcement for `manager install`.
- Global `--scope` and `--format` placement across package and manager commands.
- Global `--allow-hook-dependency-install` authorizes hook dependency
  installation for one invocation and remains off by default.
- Global `--pause` waits after non-TUI commands, writes its prompt to stderr,
  and does not contaminate TOML stdout.
- Non-applicable global options are accepted and ignored.
- Removed CLI grammar and entry points are rejected rather than redirected.
- Human/TOML stream separation and exit codes 0, 2, 3, and 4.
- Every TOML result contains the versioned common envelope and an exit code
  equal to the process status.
- Manager TOML target ordering and aggregate severity are deterministic.
- Configuration schema modes, unsafe values, legacy hints, and cross-field
  constraints.
- Wheel/sdist contents and installed entry points outside the checkout.
- Standalone assembly from the same source/version.

Use real temporary package/configuration layouts. Mock only external boundaries
such as network, subprocess/elevation, registry, and Windows integration.

### Verification

```text
.venv\Scripts\python.exe -m pytest -q
python tools\validate_registry.py pkgs
python -m build
```

Install the wheel into a clean environment and run `gupkg --version`,
`gupkg --help`, and `python -m gupkg --help` from outside the checkout. Verify
UAC, native shims, PATH propagation, shortcuts, and registry effects manually
on Windows.

## Completion checklist

- [ ] All design decisions are recorded and reflected across surfaces.
- [ ] One parser tree parses each invocation once.
- [ ] One resolver dispatches package commands and explicit `manager`
  subcommands without implicit mode selection.
- [ ] Help exposes package commands and approved manager subcommands without
  side effects.
- [ ] Options and scope values are consistent and owned by the correct parser.
- [ ] No CLI compatibility aliases or legacy entrypoints remain; the canonical
  console script and `python -m gupkg` call `cli.main` directly.
- [ ] `config-fix` creates a timestamped backup by default and honors both
  backup opt-outs.
- [ ] Registry selectors are accepted only by their owning manager subcommands.
- [ ] Machine output is one parseable, versioned document whose recorded exit
  code matches the process and whose command-specific records are deterministic.
- [ ] CLI/TUI use public shared manager operations.
- [ ] Package workflows do not depend on private update helpers.
- [ ] Configuration normalization is split by documented schema concept.
- [ ] Package layout matches build metadata and tests use normal imports.
- [ ] One version source feeds runtime, packaging, and standalone assembly.
- [ ] Built artifacts contain all required runtime/package data.
- [ ] Focused regressions, full suite, registry validation, build, installed
  smoke tests, and documented manual Windows checks pass.
- [ ] User and contributor documentation describes the implemented contract.

## Non-goals

- Replacing argparse.
- Rewriting package installation, update staging, or Windows integration.
- Redesigning `pkg.toml` or canonical runtime configuration.
- Adding a package database, service container, command framework, or schema
  framework.
- Splitting cohesive workflows solely to reduce complexity metrics.
- Completing unrelated issue 012 features.
- Enforcing complexity metrics in CI.
