# Design: fixed-location manager mode and safe bulk upgrades

Date: 2026-08-20
Priority: High
Change type: Configuration, package inventory, aggregate mutation, CLI, and TUI

## Summary

`gupkg` should support two equally valid deployment styles:

1. **Package-local mode** keeps today's behavior. A package may carry or invoke
   its own `gupkg`, and commands operate on that package or on an explicitly
   supplied package path.
2. **Manager mode** uses one centrally installed `gupkg`. A small
   `gupkg-config.toml` identifies the user and system package roots. Running
   `gupkg` from the directory containing that file opens a manager interface
   that inventories both roots, reports installation and update state, opens
   the ordinary package UI for one target, and can safely upgrade all eligible
   installed packages.

The manager is an orchestrator over the existing single-package operations. It
must not introduce a second package format or a second implementation of
install and upgrade behavior.

This issue extends the installable application and collection discovery work
described by `issue010-gupkg-installable-collection-mode-blueprint.md`. The
current code already provides an installed console entry point, manifest-based
collection discovery, aggregate read-only checks, per-package update locks,
and a basic collection selector. This design adds explicit configuration,
scope ownership, richer status, and the first aggregate mutation.

This document is an implementation plan. It does not implement the feature.

## Goals

- Install `gupkg` once and use it independently of package directories.
- Retain package-local operation without requiring migration.
- Select manager mode explicitly and predictably through
  `gupkg-config.toml`.
- Configure one system package root and one per-user package root, including
  paths containing Windows environment variables such as `%USERPROFILE%`.
- Present a deterministic, scrollable inventory with scope, installed version,
  locally available version, health, and update status.
- Let an inventory row enter the established per-package operation UI.
- Add a safe `upgrade all` workflow for installed packages.
- Continue after an individual package failure and provide a complete summary.
- Preserve the current exit-code and `ActionResult` conventions.
- Make interactive and noninteractive manager behavior use the same domain
  operations.

## Non-goals

- Removing package-local launchers or embedded copies.
- Changing `pkg.toml`, version-directory names, `current`, `pkg.local`, or the
  update-provider protocol.
- Downloading a package catalog from the internet.
- Treating arbitrary descendants as packages.
- Installing every uninstalled package in one operation.
- Removing packages or deleting historical versions.
- Making a multi-package upgrade globally atomic.
- Running scheduled or background upgrades.
- Silently elevating individual package operations midway through a batch.
- Adding a database or a second source of truth for installed versions.

## Terminology

- **Manager directory**: the directory containing `gupkg-config.toml`.
- **Configured root**: either the user or system package collection path from
  the manager configuration.
- **Target**: one discovered package root together with its configured scope.
  `user:vscode` and `system:vscode` are different targets even when their
  selectors match.
- **Available package**: a manifest-backed package discovered in a configured
  root. It may or may not be installed.
- **Installed package**: a target whose `current` entry validly activates one
  of that target's version directories.
- **Locally available version**: the greatest non-bootstrap manifest-backed
  version already present under the package root, or the bootstrap version if
  no release version exists.
- **Upgrade plan**: a non-installing snapshot of eligible targets and their
  update check outcomes. Checks may update package-owned check state and use a
  transient lock; the plan does not download or activate a package. It is
  advisory, and every target is revalidated before mutation.
- **Package-local mode**: ordinary single-package behavior, regardless of
  whether the executable is physically copied into the package or comes from
  an installed command.
- **Ad-hoc collection mode**: the existing unconfigured collection behavior
  from issue 010. It remains read-only in this feature.

## User-facing modes

The executable's physical location and its invocation mode are separate. A
centrally installed executable can still be given one package path, and a
package-local launcher can still explicitly load a manager configuration.

| Context | Bare `gupkg` behavior | Mutating scope |
| --- | --- | --- |
| Directory with valid `gupkg-config.toml` and `mode = "manager"` | Manager TUI | Determined by the target's configured root |
| Resolvable package root/version/current | Existing package TUI | Existing package rules |
| Other directory | Existing ad-hoc collection TUI | No bulk mutation |
| Explicit package path or `--package` | Existing package command/TUI | Explicit selection rules |
| Explicit `--config PATH` | Manager behavior for that file | Determined by configured root |

Manager mode is not inferred merely because two familiar directories exist.
The marker is required so a mistyped working directory cannot broaden a
single-package command into a batch operation.

## Manager configuration contract

### Minimal file

The first schema should stay deliberately small:

```toml
mode = "manager"
schema_version = 1

[packages]
system = 'D:\Programs'
user = '%USERPROFILE%\Programs'
```

TOML literal strings are recommended for Windows paths because backslashes do
not need escaping. Both keys are required in schema version 1. A user who does
not want packages in one scope may point that key at an existing empty
directory. Requiring both keeps the manager's scope model unambiguous and
avoids configuration-dependent field shapes.

The names are intentionally `system` and `user` in the public file. Internally
they map to `Scope.MACHINE` and `Scope.USER`, respectively; the existing enum
does not need a compatibility rename.

### File discovery and precedence

Manager configuration is selected in this order:

1. `--config PATH` selects that exact regular file.
2. Otherwise, `<cwd>/gupkg-config.toml` is considered.
3. The application does not search parent directories.

Parent searching would make a package command change meaning when a distant
ancestor gains a manager file. Callers in nested directories can use
`--config` explicitly.

An explicit package target has priority over implicit current-directory mode.
For example, from a manager directory,
`gupkg install C:\staging\tool\v1.0.l1` remains an explicit single-package
operation. `--config` and an unrelated explicit package path are rejected
unless the package was selected from that manager inventory.

If `gupkg-config.toml` exists but is malformed, has an unsupported mode, or is
invalid, invocation fails with exit code 2. It must not fall through to
package or ad-hoc collection mode, because a broken safety marker should be
visible.

### Path expansion

Configured paths are processed without invoking a shell:

1. Parse the TOML value as a string.
2. Expand `%NAME%` using the process environment. Windows environment names
   are matched case-insensitively.
3. Expand a leading `~` using the current user's home directory.
4. Resolve a relative value against the manager directory, not the caller's
   later working directory.
5. normalize the result to an absolute path for comparison and display.

Unknown `%NAME%` references are configuration errors that name the field and
variable. They must not be left as literal path characters. Command
substitution, registry lookup, PowerShell expressions, and package variables
such as `$App` are not supported in this file.

Examples:

```toml
[packages]
system = '%ProgramData%\gupkg\packages'
user = '%USERPROFILE%\opt'
```

### Validation

Validation occurs before discovery or mutation:

- the top level permits only `mode`, `schema_version`, and `packages`;
- `mode` must be exactly `manager`;
- `schema_version` must be the integer `1`;
- `[packages]` must contain nonempty string values for exactly `system` and
  `user`;
- expansion must resolve every environment reference;
- each resolved path must denote a directory when it exists;
- the two resolved roots must not be equal;
- neither configured root may contain the other; and
- the manager file must be a regular file.

Equal or nested roots are rejected because they can discover one physical
package twice with conflicting scope. A missing, non-directory, or unreadable
root is reported as an incomplete scope rather than preventing the manager TUI
from opening. Non-installing views can then explain the problem. Mutations
require every selected scope to be complete.

Configuration loading never creates roots. A later convenience command may
offer `gupkg manager init --create-roots`, but implicit directory creation is
not part of first-run discovery.

## Meaning of the configured roots

Each configured path is a collection root containing package roots:

```text
D:\Programs\
  vscode\
    vbootstrap.l1\pkg.toml
    v1.130.0.l1\pkg.toml
    current\
  vlc\
    vbootstrap.l1\pkg.toml
```

The package definition, downloaded versions, `current`, and `.gupkg` state
remain together under each package root. Manager mode does not copy definitions
into a separate installation database.

The existing collection discovery contract is reused independently for each
configured root, including `gupkg-dir.toml` grouping markers, finite depth,
deterministic ordering, no descent into package payloads, and preservation of
scan diagnostics. The root's configured scope is attached after discovery.

The configured scope is authoritative for all selected operations:

- a target found under `[packages].user` always passes `Scope.USER`;
- a target found under `[packages].system` always passes `Scope.MACHINE`;
- manager operations never use `Scope.AUTO`.

This prevents an elevated process from accidentally installing a user-root
target with machine-level shortcuts or environment entries.

## Inventory and status model

### Target identity and collisions

The stable target ID is `<scope>:<selector>`, for example:

```text
user:vscode
system:editors/vscode
```

Selectors retain the collection-relative rules from issue 010. Sorting is by
selector, then user before system, using case-insensitive Windows ordering with
original spelling as a tie-breaker.

The same selector may exist in both roots and produces two visibly scoped rows.
The manager does not merge manifests or guess that the two roots represent one
installation. A short `--package vscode` selection is accepted only when it is
unique across selected scopes; otherwise `--scope user|system` or a full target
ID is required.

### Status dimensions

Do not compress all state into one vague status string. A target exposes these
dimensions:

| Dimension | Values | Source |
| --- | --- | --- |
| Scope | `User`, `System` | Manager config root |
| Installation | `installed`, `not-installed`, `broken` | Strict inspection of `current` |
| Installed version | `v<upstream>.l<local>` or none | Target of valid `current` |
| Local version | Greatest manifest-backed version | Package directories |
| Health | `healthy`, `unhealthy`, `incomplete` | Manifest/layout validation |
| Update | `unchecked`, `current`, `available`, `not-configured`, `error` | Explicit update check |
| Candidate | Candidate version or none | Update check result |

`current` is the installation authority. A sole version directory without a
`current` junction remains `not-installed`, even though existing explicit-path
resolution may permit operating on it. A missing target, a target outside the
package root, or an invalid activation entry is `broken`, not silently
`not-installed`.

Bootstrap packages are available definitions. They are installed only if
`current` points to the bootstrap version. They remain eligible for the normal
bootstrap promotion logic.

Local inventory never performs a network request. Update state begins as
`unchecked`; `Refresh update status`, `upgrade check`, or the planning stage of
`upgrade all` populates it. Persisted package-owned update state may be shown as
`last checked` information, but stale state must not be presented as a fresh
check.

### Suggested runtime records

Keep the representation small and result-oriented:

```python
@dataclass(frozen=True)
class ManagerConfig:
    path: Path
    system_root: Path
    user_root: Path


@dataclass
class ManagedTarget:
    target_id: str
    scope: Scope
    package: DiscoveredPackage
    installation_status: str
    installed_version: str | None
    local_version: str | None
    health_status: str
    update_status: str = "unchecked"
    candidate_version: str | None = None
    diagnostics: list[str] = field(default_factory=list)
```

These are not required public APIs. The important contract is the observable
state, not exact class names or field layout.

## CLI contract

### Global selection

Add:

```text
--config PATH
--scope user|system|all
```

In manager mode `--scope` defaults to `all` for read-only commands. For
single-package selection it disambiguates duplicate selectors. Existing
package-local `--scope Auto|User|Machine` remains supported by the package
parser; implementation should avoid exposing two conflicting parsers in one
invocation.

The following manager commands are the first-release contract:

```text
gupkg list [--scope user|system|all]
           [--filter all|installed|uninstalled|updatable|unhealthy]
           [--toml]

gupkg upgrade check [--scope user|system|all] [--toml]

gupkg upgrade all [--scope user|system|all]
                  [--yes]
                  [--dry-run]
                  [--fail-fast]
                  [--local-deps-autoinstall]
                  [--toml]

gupkg doctor [--scope user|system|all] [--toml]

gupkg --package SELECTOR [--scope user|system] [package command...]
```

`list` reports local state only except for the `updatable` filter. Because that
filter cannot be answered truthfully without a check, it performs update
checks and labels failures rather than relying on an indefinite cache.

`doctor` validates the manager file, expanded roots, discovery completeness,
duplicate/ambiguous targets, activation entries, and manifests. It does not
contact update providers or mutate packages.

### Machine-readable output

Manager `--toml` output should have one stable document rather than mixed
single-package footers:

```toml
[manager]
schema_version = 1
config = "C:\\manager\\gupkg-config.toml"
complete = true

[[target]]
id = "user:vscode"
selector = "vscode"
scope = "user"
path = "C:\\Users\\me\\Programs\\vscode"
installation = "installed"
installed_version = "v1.130.0.l1"
local_version = "v1.130.0.l1"
health = "healthy"
update = "current"
candidate_version = ""
changed = false
errors = []
warnings = []

[summary]
total = 1
installed = 1
upgraded = 0
current = 1
skipped = 0
failed = 0
```

Use an empty string consistently for absent optional scalar values if the
current TOML emitter cannot omit fields. Do not print progress logs to stdout
when `--toml` is active; diagnostics belong in the document or on stderr.

### Exit status

Preserve the existing meanings:

- `0`: every selected operation completed successfully;
- `2`: invalid configuration, incomplete selected inventory, unhealthy target,
  ambiguous selection, or refused confirmation;
- `3`: update provider, download, activation, lock, elevation, or other
  operational failure;
- `4`: unexpected internal failure.

Batch work attempts every eligible target unless `--fail-fast` was requested.
The returned code is the most severe result encountered, ordered `4`, `3`,
`2`, then `0`. Skipped uninstalled or not-configured packages are not failures
when the reason is reported.

## Manager TUI

The TUI follows `docs/tui_style_guide.md`: a plain vertical reading order, one
borderless `OptionList` per decision, no decorative header/footer, keyboard and
mouse selection, natural scrolling, and separate scrollable output for long
operations.

### Home screen

```text
Manager: C:\manager\gupkg-config.toml
2 roots  18 packages  12 installed  1 unhealthy

Browse packages
Refresh update status
Upgrade all installed packages
Doctor: validate manager and packages
gupkg version
```

The counts come from local inspection. Update counts appear only after an
explicit refresh. Missing or incomplete roots produce a concise warning above
the list and remain explorable through Doctor.

### Package browser

The package browser is a scrollable `OptionList`, not a table widget. Each row
is understandable without color:

```text
Filter: All
Scope: All
--- Packages ---
vscode                 User    Installed v1.130.0.l1   Update available v1.131.0
vlc                    System  Installed v3.0.22.l1    Current
windirstat             User    Not installed           Update unchecked
broken-tool            System  Broken current          Unhealthy
```

Long selectors clip visually but remain available in a detail view. Filter and
scope rows cycle in place and retain focus. Filters include All, Installed,
Uninstalled, Updatable, and Unhealthy. Searching by selector or description is
a reasonable follow-up and may be included if Textual's input behavior remains
simple; it is not required for the first release.

Selecting a row opens a package detail screen showing full target ID, paths,
description, versions, health, update state, and diagnostics. From there the
user can open the existing package operation UI. The configured scope is
preselected and locked for that target; an operation must not drift from the
root that supplied the row. An uninstalled target can therefore be installed
individually without adding `install all`.

### Refresh behavior

Filesystem inventory refresh is cheap and occurs when returning from a package
operation. Network update status is refreshed only on request. Long-running
checks run outside Textual's event loop, open a result/progress screen first,
and update rows as results arrive. Cancellation stops scheduling new checks but
does not interrupt an update hook at an unsafe point.

### Upgrade-all flow

Selecting `Upgrade all installed packages` has two stages:

1. **Plan** performs non-installing update checks for healthy installed targets
   and presents counts for available, current, skipped, and failed checks.
2. **Confirm and run** displays `Run planned upgrades` first, followed by scope,
   checksum, dependency, and fail-fast settings. The user must activate Run;
   opening the screen alone changes nothing.

The result view streams one textual status per target and ends with a summary.
Returning to the manager refreshes local installation state so newly activated
versions are immediately visible.

## `upgrade all` behavior

### Eligibility

A target is eligible only when all of these are true:

- it is in the selected scope;
- its configured root was scanned completely;
- it is installed through a valid `current` activation entry;
- its relevant manifest and package layout are healthy;
- it declares update configuration; and
- the update check reports a newer candidate.

Uninstalled targets, broken activations, unhealthy packages, packages without
update configuration, and packages reported current are skipped with distinct
reasons. `upgrade all` never turns package availability into implicit consent
to install new software.

Historical manifests may still be checked by the aggregate diagnostic command,
but batch mutation operates once per target from its installed version or
valid bootstrap activation. It must not upgrade every historical manifest.

### Planning and confirmation

`gupkg upgrade all` first performs the non-installing plan. In an interactive
terminal it prints the plan and asks for one confirmation. In a noninteractive
terminal it refuses mutation unless `--yes` is supplied. `--dry-run` prints the
same plan and never prompts, downloads, activates, or changes installation
components. Update checks may still refresh package-owned check state.

The plan is a snapshot, not a promise that the candidate will remain unchanged.
Before each mutation, the target's path, scope ownership, `current`, and health
are checked again. The existing `full_package_upgrade` operation may recheck
the provider and must remain authoritative if the candidate changed.

### Ordering and concurrency

First-release mutations are sequential. Deterministic order is:

1. user targets by canonical selector;
2. system targets by canonical selector.

Sequential execution keeps logs readable, respects package-local locks, avoids
simultaneous hook dependency installation, and reduces registry and PATH
contention. Read-only update checks may gain bounded concurrency later, but the
initial implementation should favor determinism.

### Elevation

User targets never require elevation merely because the manager process is
elevated. System targets require an elevated process before mutation begins.

If a confirmed plan contains system targets and the process is not elevated,
the TUI offers to relaunch the whole confirmed batch once through the existing
Windows elevation boundary. No user target is mutated before that decision, so
the elevated rerun cannot duplicate partial work. A noninteractive invocation
fails with exit code 3 and an actionable instruction unless it was already
started elevated.

The relaunched command carries the resolved config path, selected scopes,
batch flags, and a private confirmation token or equivalent internal marker.
It must not reconstruct arguments as shell text. The implementation should
avoid persisting secrets because this configuration contains only paths.

### Failure, recovery, and rollback

Each package retains its existing update lock, staging directory, receipt, and
activation semantics. The batch does not add a global lock and is not globally
atomic:

- a failure in one target does not roll back targets already upgraded;
- by default, later targets are still attempted;
- `--fail-fast` stops scheduling later targets after the first failure;
- a failed target retains only the recovery state already defined by its
  single-package operation;
- rerunning `upgrade all` safely rechecks every target and skips those now
  current; and
- the final summary distinguishes upgraded, current, skipped, failed, and not
  attempted targets.

This is preferable to inventing a cross-package rollback that cannot reliably
undo external installers, registry changes, shortcuts, or package-local hooks.

## Mode resolution and dispatcher changes

The current dispatcher discovers a collection before determining package
context. Manager configuration should be resolved early enough that its errors
are authoritative, while retaining explicit single-package commands.

Recommended resolution order:

1. Parse only truly global selectors: `--config`, `--package`, manager
   `--scope`, `--root`, `--max-depth`, and output mode.
2. Recognize an explicit positional package path and route it to package mode.
3. Load an explicitly selected manager config, if any.
4. Otherwise inspect the exact working directory for `gupkg-config.toml`.
5. If a valid manager config is active, build the two-root manager inventory.
6. Otherwise, if the root resolves as a package, use package mode.
7. Otherwise retain ad-hoc collection mode.

Do not make the manager parser duplicate the entire package parser. Once a
manager target is selected, construct the same argument list or call the same
domain entry point used by package mode, adding the target's explicit scope.

## Implementation boundaries

### New manager domain

Add a focused `src/gupkg/manager.py` module responsible for:

- reading and validating `gupkg-config.toml`;
- safe environment and relative-path expansion;
- discovering both configured roots through `discover_collection`;
- attaching explicit scopes and calculating local installation status;
- selecting a unique managed target;
- constructing upgrade plans; and
- executing batch plans through existing package operations.

The module should expose a few meaningful workflows rather than registries,
provider abstractions, or many one-use helpers. Filesystem discovery remains in
`collection.py`; package config parsing remains in `configuration.py`; package
path and `current` rules remain in `layout.py`; update and install behavior
remain in `gupkg.py`/the existing operation modules.

If calling `full_package_upgrade` from `manager.py` creates an import cycle,
move the public single-package operation functions to a small operation module
as a separate refactor. Do not copy their logic into manager code.

### TUI adapter

Add `src/gupkg/manager_tui.py` rather than overloading the current minimal
`collection_tui.py` with configured-root and mutation state. It may reuse
small presentation records, but the domain inventory must remain usable without
Textual imports.

Extend `run_tui` with an optional forced scope or introduce an equivalent
package-UI context. Manager-selected targets pass `Scope.USER` or
`Scope.MACHINE`; ordinary package-local calls retain current automatic behavior.
The locked scope must be visible in the operation UI.

### Installed-state inspection

Add or expose one layout-level function that strictly inspects `current`
without falling back to the sole version directory. This is distinct from
`resolve_input_path`, whose fallback is useful for explicit install commands.
It should return a result that can distinguish absent and broken activation.

Use the existing semantic package-version comparator for installed and local
versions. Do not compare version-directory names lexically.

### Output and results

Create one aggregate result record containing per-target `ActionResult` values
and skip reasons. Human and TOML renderers consume that record. They must not
re-run discovery or infer success from log text.

Continue using package-root update locks. A manager-wide database, cache, or
lock is unnecessary in the first release.

## Safety requirements

- No mutating manager command runs merely because `gupkg-config.toml` exists.
- `upgrade all` requires explicit interactive confirmation or `--yes`.
- Uninstalled packages are never included in a batch upgrade.
- Manager targets always use the scope of their configured root, never Auto.
- Unknown environment variables stop config loading.
- Equal or nested configured roots are rejected.
- Incomplete selected roots block batch mutation.
- Package paths are revalidated beneath their configured roots immediately
  before mutation.
- Directory symlink and reparse-point discovery rules remain those of issue
  010; manager mode does not widen traversal.
- Existing package locks serialize concurrent update work per target.
- Batch argument relaunch uses an argument vector, never shell interpolation.
- `--no-checksum` and `--local-deps-autoinstall` apply visibly to the whole
  batch and are off by default.
- One package failure cannot hide outcomes for packages already attempted.

## Performance and responsiveness

- Local inventory is proportional to the two bounded collection scans and does
  not contact the network.
- Inventory order is deterministic regardless of filesystem enumeration order.
- The TUI opens after local discovery; network checks run in workers and do not
  block terminal rendering.
- Mutations are sequential in the first release.
- Status is refreshed incrementally in the TUI, but final summaries are
  rendered from the completed aggregate result.

## Test strategy

Tests must protect observable contracts in accordance with `docs/tests.md`.
Use real temporary directory layouts and TOML files. Mock only real boundaries
such as update providers, elevation, subprocess launch, registry access, and
Textual execution. Do not test private helper call order or exact class layout.

Every permanent test module and test function must have the required
behavior-oriented docstrings.

### Configuration behavior

Protect these behaviors:

- a valid manager file selects manager mode;
- `--config` loads a file outside the current directory;
- parent directories are not searched implicitly;
- malformed, unknown-key, wrong-mode, and unsupported-version files fail with
  actionable exit-code-2 errors;
- `%USERPROFILE%` expansion is case-insensitive on Windows;
- an unknown environment variable fails instead of becoming a literal path;
- relative roots resolve from the manager directory;
- equal and nested roots are rejected; and
- a missing/unreadable root remains visible as an incomplete scope.

### Inventory behavior

Protect these behaviors:

- packages from both roots receive the correct scope and stable target ID;
- duplicate selectors across scopes remain separate rows;
- package selection requires scope when a selector is ambiguous;
- valid `current` reports the installed semantic version;
- missing `current` reports not installed even for a sole version directory;
- broken/out-of-root `current` reports broken;
- bootstrap-only packages appear as available; and
- sorting and summary counts are deterministic.

### CLI behavior

Protect these behaviors:

- bare invocation in a manager directory opens the manager entry point;
- explicit package paths retain package mode;
- `list` filters and TOML output reflect observable target state;
- `doctor` reports all root, activation, and manifest diagnostics;
- a manager-selected user target passes User scope even under an elevated
  process;
- a manager-selected system target passes Machine scope;
- noninteractive `upgrade all` refuses mutation without `--yes`;
- `--dry-run` checks and plans but does not download, install, or elevate; and
- aggregate exit status uses the most severe completed result.

### Batch-upgrade behavior

Before adding each test, state the user-visible regression it prevents. Cover:

- only healthy installed targets with available updates are mutated;
- uninstalled, broken, current, unhealthy, and not-configured targets receive
  distinct skip results;
- targets run in deterministic user-then-system order;
- a failure does not stop later targets by default;
- `--fail-fast` leaves later targets explicitly not attempted;
- each target is revalidated before its mutation;
- rerunning after a partial success skips packages already current;
- package-local update locks still reject concurrent work;
- a mixed-scope batch requests elevation before any mutation;
- declining or failing elevation performs no mutation; and
- no cross-package rollback is attempted after a later failure.

Integration tests should call the public manager/CLI boundary with fake update
providers and real package directories. They should not assert which private
helper was called.

### TUI behavior

Use Textual's test driver for a small number of durable interaction contracts:

- the inventory is scrollable and every target is keyboard reachable;
- scope/filter changes visibly change rows without losing focus;
- duplicate selectors show distinct scope labels;
- selecting a target opens details and preserves its forced scope;
- planning alone does not download or activate a package;
- the confirmed upgrade opens a responsive result view; and
- returning after an operation refreshes installed versions.

Avoid screenshot snapshots, widget-tree shape assertions, exact spacing, and
tests that freeze implementation-only screen structure.

## Documentation and migration

Update the root README when implemented with:

- package-local versus manager-mode examples;
- installation of one central command through pipx, uv, or pip;
- the complete `gupkg-config.toml` schema and path-expansion rules;
- package root examples for user and system scope;
- manager CLI and TUI workflows;
- `upgrade all` confirmation, elevation, skip, and partial-failure semantics;
- explicit recovery guidance: fix the failed package and rerun; and
- a warning that manager roots own scope and should not be nested.

Migration is opt-in:

1. Install `gupkg` centrally.
2. Choose or retain existing user and system collection roots.
3. Create a manager directory and `gupkg-config.toml`.
4. Run `gupkg doctor` and `gupkg list` before the first batch mutation.
5. Keep or remove package-local launchers at the user's discretion.

No package contents need to change. Existing `current`, version directories,
manifests, update state, and launchers remain valid.

## Delivery phases

### Phase 1: configuration and mode selection

1. Implement strict manager config loading and path expansion.
2. Add `--config` and deterministic mode resolution.
3. Add tests for precedence, malformed markers, variables, and path safety.

Exit criterion: a valid marker reliably selects two resolved roots, while all
existing package and ad-hoc collection invocations remain compatible.

### Phase 2: scoped inventory and CLI

1. Build the two-root managed inventory on existing discovery.
2. Add strict installed-state inspection and status dimensions.
3. Implement selection, list filters, doctor, aggregate result records, and
   stable TOML output.

Exit criterion: users and scripts can truthfully inspect both roots and select
one scoped target without mutation.

### Phase 3: manager TUI

1. Add the manager home and scrollable package browser.
2. Add filtering, details, diagnostics, and explicit update refresh.
3. Enter the existing package UI with a visible forced scope.

Exit criterion: keyboard and mouse users can find every package, understand
its state, and operate on it individually.

### Phase 4: upgrade planning and batch mutation

1. Build an immutable/advisory plan from current inventory and update checks.
2. Add CLI/TUI confirmation and dry-run.
3. Execute sequentially with revalidation, package locks, elevation preflight,
   fail-fast, and complete summaries.
4. Add partial-failure and retry integration tests.

Exit criterion: one confirmed command safely upgrades all eligible installed
targets and truthfully reports every outcome.

### Phase 5: documentation and hardening

1. Update user and development documentation.
2. Exercise user-only, system-only, duplicate-selector, incomplete-root,
   bootstrap, mixed-scope elevation, and partial-failure smoke scenarios.
3. Verify narrow/short terminal behavior and machine-readable output.

Exit criterion: the central-manager workflow is documented, recoverable, and
does not require a copied installer in package directories.

## Acceptance criteria

1. A package-local invocation behaves as it did before this feature.
2. `gupkg-config.toml` with `mode = "manager"` selects manager mode only from
   its own directory or through `--config`.
3. The config accepts `%USERPROFILE%`-style variables and rejects unresolved
   variables.
4. User and system roots are validated, non-overlapping, and visibly scoped.
5. The manager inventory lists every manifest-backed available package from
   both roots in deterministic order.
6. Every row reports scope, installed/broken/not-installed state, installed
   version when present, local version, health, and update state.
7. Duplicate package selectors in different scopes remain distinguishable.
8. The TUI package list scrolls and is fully usable with Up, Down, Enter, and
   Escape.
9. Selecting a row enters package operations with the configured scope locked.
10. `gupkg doctor` reports config, root, discovery, activation, and manifest
    problems without mutation or network access.
11. `upgrade all` plans only healthy installed targets and never installs an
    uninstalled package.
12. Interactive batch mutation requires confirmation; noninteractive mutation
    requires `--yes`; dry-run never mutates.
13. System mutation is elevated before any batch target is changed.
14. Targets are revalidated and upgraded sequentially through existing
    single-package operations.
15. One failure is isolated, later targets continue by default, and the final
    result uses the most severe exit status.
16. Rerunning after partial success is safe and skips packages already current.
17. TOML output is parseable, stable, and contains per-target results plus a
    summary without interleaved stdout logs.
18. Existing package-local, explicit-path, and ad-hoc collection tests continue
    to pass.

## Recommended follow-ups

These are useful but should not delay the core design:

- substring search in the package browser;
- `upgrade selected` for a multi-select review screen;
- `manager init` and `manager config show` convenience commands;
- bounded parallel update checks with deterministic final rendering;
- an opt-in freshness duration for displaying cached update status;
- export of a plain inventory report; and
- scheduled checks that notify but never install without a separately designed
  policy and credential/elevation model.

Bulk install, automatic removal, pruning old versions, and unattended upgrade
policy each deserve separate safety designs rather than being hidden inside
manager mode.
