# Blueprint: rename `pkg` to installable `gupkg` and add collection mode

Date: 2026-08-03
Priority: High
Change type: Product rename, packaging, package discovery, aggregate CLI, and TUI design

## Goal

Turn the source-tree tool currently named `pkg` into an installable Python
application named `gupkg`, and let one installed copy manage either one package
or a directory containing many packages.

The completed application must support all of these entry points with the same
behavior:

```text
gupkg
python -m gupkg
gupkg --package vscode
python -m gupkg --package vscode
```

When invoked in one package, `gupkg` presents or runs that package's existing
operations. When invoked from a containing directory, it discovers package
roots, presents collection-wide read-only operations, and lets the user choose
a package before entering the same per-package menu.

This document is a design only. It does not authorize implementation as part of
this issue-writing task.

## Requested outcomes

1. The project, Python import package, command, user-facing text, and installed
   application are named `gupkg`.
2. A wheel or editable install provides both the `gupkg` console command and
   `python -m gupkg`; programs no longer carry a copy of the manager's source.
3. A directory containing package folders can be used as a collection root.
4. Direct child folders are considered without an unrestricted recursive walk.
5. A `gupkg-dir.toml` marker explicitly permits recursion through a grouping
   directory, including when the marker file is empty.
6. Collection health checks cover every discovered `pkg.toml`.
7. Collection update-availability checks cover every valid discovered
   manifest that declares update configuration, including historical versions.
8. `gupkg --package <name>` selects a discovered package and opens its ordinary
   package menu. The selector also works with noninteractive package commands.
9. The collection TUI lists all, updatable, or unhealthy packages and opens the
   existing package operation menu when a package row is selected or clicked.

## Terminology

This design uses the following terms consistently.

- **Manifest**: one canonical `pkg.toml` in a version directory.
- **Version directory**: an immediate package-root child named
  `v<upstream>.l<local>`, as recognized by the existing layout rules.
- **Package root**: a directory whose immediate version-directory children
  contain one or more manifests. It may also contain `current` and `.pkg` or
  `.gupkg` manager state.
- **Collection root**: the directory from which discovery begins, normally the
  caller's current working directory or an explicit `--root` path.
- **Grouping directory**: a non-package directory containing the regular file
  `gupkg-dir.toml`. Its immediate children are searched using the same rules as
  the collection root.
- **Selector**: the stable collection-relative identifier used by
  `--package`, such as `vscode` or `editors/vscode`.
- **Operational version**: the one version on which a package-wide action can
  operate without guessing. It is `current` when valid, or the sole discovered
  version when no `current` exists.
- **Inventory**: the in-memory discovery result containing packages, manifests,
  status, diagnostics, and any branches that could not be completely scanned.

## Product and compatibility decisions

### Rename the application, not the manifest format

The new names are:

| Concern | New canonical name |
| --- | --- |
| Distribution/project | `gupkg` |
| Import package | `gupkg` |
| Console command | `gupkg` |
| Module invocation | `python -m gupkg` |
| Nested collection marker | `gupkg-dir.toml` |
| Manager-owned package state | `.gupkg` |
| Per-user dependency environment | `%LOCALAPPDATA%\gupkg\dependencies` |

The following package-definition names remain unchanged in this release:

- `pkg.toml`
- `pkg.local/`
- `PKG_MODULE_API`
- the hook payload's `PkgVars` key
- package variables such as `$App`, `$Icons`, and `$Shortcuts`

Keeping `pkg.toml` is required by collection discovery and avoids forcing every
managed program to rewrite its manifest during the application rename.
`pkg.local` and the hook protocol are package-format names rather than the
installed Python import namespace. Renaming them at the same time would add an
unrelated hook-API migration.

### Compatibility window

The installed distribution exposes only `gupkg`, not a second `pkg` console
script or top-level `pkg` import. A generic `pkg` name is collision-prone and a
permanent alias would leave the rename incomplete.

For one documented transition release, repository launchers may retain:

- `src/pkg.cmd` as a thin deprecated shim that executes `python -m gupkg`;
- `PKG_PYTHON` and `pkg.python` as deprecated launcher fallbacks after the new
  `GUPKG_PYTHON` and `gupkg.python` names;
- a clear warning naming the replacement command.

No compatibility source package named `pkg` should be installed. Tests and
package-local code in this repository must move imports to `gupkg`.

### Private state migration

New update locks, receipts, work trees, and state live under `.gupkg`.

State migration must be conservative:

1. Prefer `.gupkg` when it exists.
2. If only legacy `.pkg` exists, read it for compatibility.
3. Before the first state-mutating update operation, rename `.pkg` to `.gupkg`
   atomically within the same package root.
4. If both directories exist, do not merge or delete either one. Stop the
   mutating operation with an actionable conflict message.
5. Read-only discovery and health checks never migrate state.

The old per-user virtual environment should not be moved because virtual
environments can contain absolute paths. `gupkg` creates its own environment
when package-local dependency auto-installation is used. Documentation may
tell users that the old `%LOCALAPPDATA%\pkg\dependencies` directory can be
removed manually after migration, but the application must not delete it.

## Installation and module execution

### Distribution layout

Add standard Python packaging metadata at the repository root. The target
shape is:

```text
pyproject.toml
src/
  gupkg/
    __init__.py
    __main__.py
    cli.py or gupkg.py
    ...existing implementation domains...
```

The distribution requires Python 3.11 or newer, matching the current project.
The build configuration must discover packages from `src/`, include any runtime
resources, and define this console entry point:

```toml
[project.scripts]
gupkg = "gupkg.cli:main"
```

The exact facade module may remain `gupkg.gupkg` instead of `gupkg.cli` if that
keeps the current high-level workflow easier to read. There must still be only
one public `main(argv=None) -> int` command dispatcher.

`gupkg/__main__.py` is a trivial wrapper around the same dispatcher:

```python
from .cli import main

raise SystemExit(main())
```

The console entry point and module entry point must not have separate parsers,
defaults, logging, or error handling.

### Supported installation workflows

Document at least these workflows:

```text
python -m pip install .
python -m pip install -e .
pipx install .
uv tool install .
```

The first is an ordinary environment install, the second is for contributors,
and the tool installers are the preferred way to make `gupkg` globally
available without modifying a shared Python environment.

Publishing to a public package index is a separate release decision. Local
installation from a checkout must work without publication.

### TUI dependency

Because no-argument `gupkg` is an interactive entry point, Textual should be a
declared runtime dependency of the installed application. The application
must not download its own UI framework on first launch.

The isolated dependency environment remains only for explicitly authorized
missing dependencies of trusted `pkg.local` hooks. This keeps installed
application dependencies under the selected installer while preserving the
current opt-in hook behavior.

### Installed-code execution

The TUI currently launches the adjacent `pkg.py` file. After packaging, child
commands must use the installed interpreter and module:

```text
<sys.executable> -m gupkg <arguments>
```

This guarantees that a TUI opened from any collection uses the same installed
version as the parent process and does not depend on a copied source tree.

## Invocation modes

The dispatcher determines mode from explicit selection first and filesystem
context second.

| Invocation | Result |
| --- | --- |
| `gupkg` in a resolvable package | Open the per-package TUI. |
| `gupkg` in a collection or marked empty collection | Open the collection TUI. |
| `gupkg --package vscode` | Discover from the current directory and open `vscode` in the per-package TUI. |
| `gupkg --root D:\Apps --package editors/vscode` | Discover from `D:\Apps` and open the selected package. |
| `gupkg install` in a package | Run the existing install operation. |
| `gupkg --package vscode upgrade check` | Run the existing check for only `vscode`. |
| `gupkg config check` in a collection | Run aggregate manifest health. |
| `gupkg upgrade check` in a collection | Check update availability from every eligible discovered manifest. |
| `gupkg list` | Print the collection inventory; default filter is `all`. |

Mode resolution order is:

1. `--package` selects package mode from a collection inventory.
2. An explicit positional package path selects package mode.
3. If the current or `--root` path resolves through the existing package layout
   resolver, use package mode.
4. Otherwise use collection mode, even if the resulting inventory is empty.

An empty unrelated directory and an explicitly marked empty collection both
produce an empty collection inventory. The marker matters to a parent scan and
to UI labeling, not to whether `gupkg list` is allowed.

The current implicit no-argument install changes to the context-sensitive TUI.
Installation remains available as the explicit and script-friendly
`gupkg install`. This breaking change is intentional: a no-argument collection
invocation cannot safely imply installation of every package.

Keep `gupkg tui` as an explicit spelling for scripts, shortcuts, and users who
prefer it. It uses the same context detection as bare `gupkg`.

## Command-line contract

### Global collection options

Add these global options:

| Option | Meaning |
| --- | --- |
| `--root PATH` | Collection root used for discovery; default is the caller's current directory. |
| `--package SELECTOR` | Select exactly one discovered package by canonical selector or unique short name. |
| `--max-depth N` | Maximum package/grouping-directory depth below the collection root; default `8`. |

`--max-depth` counts directory edges from the collection root. Direct children
have depth 1. The default is deliberately finite, while a value of 8 permits
normal nested grouping trees. Reaching the cap on a marked branch creates a
visible incomplete-scan diagnostic rather than silently pretending the scan
was complete.

`--root` does not replace the existing package-path arguments. It defines the
search scope used by collection mode and `--package`. A command must reject an
explicit package path combined with `--package` because two targets would be
ambiguous.

### Package selection

Every discovered package has a canonical selector equal to its path relative
to the collection root, rendered with `/` separators regardless of platform:

```text
vscode
media/vlc
editors/stable/vscode
```

Selection follows these rules:

1. Match a canonical selector case-insensitively on Windows.
2. If no canonical selector matches, match the package-root basename.
3. A basename is accepted only when it identifies exactly one package.
4. An ambiguous or missing name is a user error that prints the possible
   canonical selectors.
5. `pkg.toml`'s `name` field is never the selector authority; directory layout
   remains authoritative and metadata mismatches belong to health results.

This makes the requested `gupkg --package name` concise for common flat
collections without making nested duplicate names nondeterministic.

### Collection commands

Use the existing operation vocabulary where behavior already exists:

```text
gupkg list [--filter all|updatable|unhealthy]
gupkg config check
gupkg upgrade check
```

`list` defaults to `--filter all`.

- `list --filter all` discovers packages and performs local manifest health so
  package status is meaningful.
- `list --filter unhealthy` performs the same health work and emits only
  packages with health problems.
- `list --filter updatable` performs update checks for eligible packages before
  filtering. It must not rely on stale or undocumented persistent cache data.
- `config check` validates all discovered manifests and prints both per-package
  diagnostics and a collection summary.
- `upgrade check` checks every valid discovered manifest that declares update
  configuration, retains version-specific results, and prints the rolled-up
  package status plus any manifest diagnostics.

`list` is collection-only. If the target path resolves as a package, it returns
a user error suggesting the parent collection or an explicit `--root`; it must
not reinterpret the package's version directories as sibling packages.

All package-mutating commands remain single-package operations in the first
release:

- `install`
- `upgrade download`
- `upgrade install`
- `upgrade full`
- `config update`
- `config from-legacy`

In collection mode these commands require `--package` or an explicit package
path. Do not infer bulk installation or bulk update activation. A future batch
mutation design will need confirmation, failure recovery, ordering, and
restart semantics; those should not be improvised inside this feature.

### Ordering and output

Inventory and aggregate output must be deterministic. Sort packages by
canonical selector using case-insensitive Windows ordering with the original
spelling as a tie-breaker. Sort manifests within a package using the existing
package-version comparator rather than lexical order.

Human-readable aggregate output should include:

1. collection root and whether discovery was complete;
2. one compact result per concerned package;
3. indented version-specific diagnostics when needed;
4. totals for discovered, healthy, unhealthy, update available, current, not
   configured, skipped/ambiguous, and errored packages.

Continue processing after one malformed manifest, inaccessible branch, hook
failure, or network failure. Aggregate checking is useful only if it reports
all concerned packages in one run.

Extend `--toml` to emit a stable aggregate document instead of interleaving
unstructured summaries. The aggregate schema should contain a collection
header, repeated package tables, and nested manifest results with at least
selector, path, health, update status, operational version, manifest version,
candidate version, and diagnostic strings. Keep the existing single-package
`ok`, `changed`, and `status` fields compatible.

### Exit status

Preserve the current exit-code meanings:

- `0`: the requested scan/check completed and every concerned item succeeded;
- `2`: user/configuration/discovery completeness error, including unhealthy or
  ambiguous packages in a command that checks them;
- `3`: an operational boundary failed, such as an update network/hook check;
- `4`: unexpected internal error.

For aggregate results, return the most severe encountered category after every
item has been attempted, ordered `4`, `3`, `2`, then `0`. An empty complete
inventory is successful for `list`, `config check`, and `upgrade check`, and
the summary says `0 packages`.

## Discovery contract

### Why discovery is signature-based

Collection mode must not recursively search every descendant for a file named
`pkg.toml`. Application payloads, source checkouts, caches, and test data can
all contain unrelated TOML files. The package directory shape remains the
authority.

A package-root signature is:

```text
<candidate package root>/
  v<upstream>.l<local>/
    pkg.toml
```

At least one immediate version-directory child must contain a regular
`pkg.toml`. The manifest may be malformed and the package is still discovered;
malformation is a health result, not a reason to hide it.

Package roots with no manifests are intentionally ignored in collection mode,
even though the existing explicit-path install can use defaults for a
pre-populated, configless package. The user specifically requested manifest
discovery, and guessing every directory with a version-looking child would
create noisy false positives. Such a package remains manageable by explicit
path.

### Traversal algorithm

For a collection root `R`, discovery performs these steps:

1. Resolve and validate `R` without changing the caller's working directory.
2. Inspect each immediate child directory of `R` in deterministic order.
3. If the child has the package-root signature, record the package and all of
   its immediate version-directory manifests. Do not recurse inside it.
4. Otherwise, if the child contains a regular `gupkg-dir.toml`, record it as a
   grouping directory and apply these same steps to its immediate children.
5. Otherwise ignore the child and its entire subtree.
6. Stop marked recursion at `--max-depth`, and record an incomplete-scan
   diagnostic for every marked branch that could not be visited.

This gives the ordinary layout the requested shallow behavior:

```text
collection/
  vscode/                 # inspected and discovered
    v1.2.3.l1/pkg.toml
  random-source-tree/     # inspected, then ignored without recursion
```

The marker opts a grouping branch into deeper discovery:

```text
collection/
  work/
    gupkg-dir.toml         # may be empty
    editors/
      gupkg-dir.toml       # may also be empty
      vscode/
        v1.2.3.l1/pkg.toml
```

The marker's presence is its complete first-release contract. It must be a
regular file, an empty file is valid, and its contents are not interpreted.
Treating content as configuration now would create an accidental schema before
there is a requested setting. Documentation should reserve all marker contents
for future use.

### Boundary and error behavior

- Never follow `current`; it is an activation junction, not another version.
- Do not descend into `App`, `Icons`, `Shortcuts`, `pkg.local`, `.pkg`, or
  `.gupkg`, because discovery stops at a package root.
- Do not follow directory symlinks or reparse points encountered below the
  collection root in the first release. A linked path may still be supplied as
  the collection root itself. This avoids cycles and discovery outside the
  named scope.
- Track resolved visited directories as a second cycle defense.
- A file or directory that disappears during a scan produces a diagnostic and
  does not abort the remaining inventory.
- Permission and I/O failures make the inventory incomplete and identify the
  skipped path.
- A child that simultaneously has a package-root signature and a
  `gupkg-dir.toml` marker is a layout conflict. Record it as unhealthy and do
  not recurse into it, so nested packages are not silently hidden inside a
  package root.
- `pkg.toml` files found directly in a package root, inside `current`, or at an
  arbitrary deeper path do not satisfy the signature.
- Multiple manifests for the same version-directory path are impossible; case
  collisions or duplicate resolved package roots are diagnostics rather than
  duplicate package rows.

### Discovery examples

Given:

```text
D:\Programs\
  vscode\v1.0.l1\pkg.toml
  vlc\v3.0.l1\pkg.toml
  experiments\scratch\v1.0.l1\pkg.toml
  grouped\gupkg-dir.toml
  grouped\database\gupkg-dir.toml
  grouped\database\sqlitebrowser\v3.13.l1\pkg.toml
```

the intended inventory is:

- `vscode`
- `vlc`
- `grouped/database/sqlitebrowser`

`experiments/scratch` is ignored because `experiments` is neither a package
root nor a marked grouping directory. In the example, `grouped/database` must
contain its own marker before traversal reaches `sqlitebrowser`; marker
position, not file content, grants recursion.

## Inventory and status model

Discovery needs a few durable result concepts, not a framework or generic
repository abstraction.

A package record contains:

- canonical selector;
- package-root path;
- all discovered manifest/version records;
- resolved current target when valid;
- operational version or a reason it is ambiguous;
- package health status and diagnostics;
- rolled-up update conditions plus every manifest's update result and
  diagnostics.

A manifest record contains its version-directory identity, manifest path,
parse/validation result, and diagnostics. The inventory contains the resolved
collection root, sorted package records, grouping directories visited, and
scan-completeness diagnostics.

Dataclasses are justified for these records because they survive across
discovery, aggregate operations, filtering, and TUI rendering. Do not create
provider registries, visitor classes, a virtual filesystem, or one wrapper
type per status field.

### Health status

Health is local and manifest-oriented. Discovery followed by health evaluation
must validate every discovered manifest using the existing canonical
normalization and health rules:

- TOML parsing and strict schema;
- directory-owned metadata consistency;
- origin history and local script references;
- update table and package-local module references;
- package-root/current layout conflicts.

A package is `healthy` only when all of its discovered manifests and its layout
are healthy. It is `unhealthy` when any one fails. Keep diagnostics attached to
the particular version so a maintainer can repair it without guessing.

Configless version directories do not become manifest records and are not, by
themselves, health failures. A broken `current` target or a current target that
does not identify a usable version is a package-layout health failure.

### Update status

Update availability can involve network access or a trusted hook. Collection
mode must attempt it independently for every discovered manifest that passes
health validation and declares update configuration, including historical
versions. Invoke the existing check workflow with that exact version path; do
not change `current` merely to inspect an older definition.

Each manifest receives one status:

- `unknown`: not checked in this process yet;
- `available`: its source returned a candidate newer than that manifest;
- `current`: its configured source returned no newer candidate;
- `not-configured`: it has no update definition;
- `skipped-invalid`: health validation failed, so its update hook/provider was
  not invoked;
- `error`: configuration, dependency, hook, network, or provider check failed.

The package row rolls up those version-specific results without discarding
them. Compute the comparison baseline as the newest installed version directory
under the package root, using the existing package-version comparator. A
candidate makes the package `updatable` only when it is newer than that
baseline. A candidate reported from a historical manifest that is equal to or
older than an already installed version is retained as `superseded` detail and
does not put the package in the Updatable filter.

Several manifests may report the same candidate; collapse it to one package
summary while retaining the contributing manifest results. If they report
different candidates newer than the installed baseline, mark the package
updatable and add a disagreement diagnostic rather than choosing one for the
user. Collection mode is read-only, so it never has to guess which manifest
should drive a download.

Package update conditions are intentionally non-exclusive: a package may be
updatable and also have an error from a different manifest. It then appears in
both Updatable and Unhealthy. A package whose manifests are all
`not-configured` is not unhealthy.

Availability checks must preserve the existing guarantee that they do not
activate versions, replace `App`, or change Windows integrations. Any small
manager coordination state already required by the existing check protocol
remains allowed.

Aggregate checks run sequentially in the first release. This keeps output
deterministic and avoids concurrent package-local dependency installation,
credential prompts, rate-limit bursts, and multiple mutation locks. Parallel
checking can be designed later from measured need.

## Collection TUI

### Entry and navigation

Extend the current list-first Textual interface rather than creating a second
visual system. Follow `docs/tui_style_guide.md`: one borderless selectable list
per decision, plain status words, no dashboard panels, and Escape to go back.

The collection home screen should resemble:

```text
D:\Programs  12 packages  2 unhealthy  updates not checked
All packages: 12
Updatable packages: check required
Unhealthy packages: 2
--- Operations ---
Check health
Check update availability
Rescan
```

`All packages` is first and initially selected. The counts are text, not
color-only badges. A partial scan adds an explicit `Warning: discovery was
incomplete` line.

The package-list screen shows only the selected filter's concerned packages.
Each row contains the selector, operational version when known, health word,
and update word when checked. Long selectors clip naturally; details belong on
the package screen, not in a multi-column dashboard.

Selecting with Enter or clicking a package row opens the same per-package home
screen used when `gupkg` starts inside that package. Do not duplicate package
action definitions or settings for collection navigation. Escape returns to
the filtered package list and then to collection home.

### Filters

- **All** is the default and includes every discovered package.
- **Unhealthy** runs local health evaluation if needed, then includes packages
  with manifest, layout, discovery, or completed-update-check errors.
- **Updatable** runs update availability checks for unchecked eligible packages
  before showing only `available` results.

If an update check is canceled, already completed statuses remain visible in
memory and unchecked packages remain `unknown`. The filter screen must state
that its result is incomplete.

Status is a fresh in-memory snapshot. `Rescan` discards it and rebuilds from
the filesystem. Do not add a central collection index or long-lived cache in
the first release; package directories and manifests remain authoritative.

### Aggregate operation results

Health and update checks run in a worker so the TUI stays responsive. The
result screen shows:

1. a command summary;
2. `Running <n>/<total>: <selector>` progress;
3. scrollable per-package output;
4. the final totals and exit category.

After returning, collection counts and package rows reflect the completed
results. A single failure does not close the application or prevent checking
later packages.

### Package menus with ambiguous versions

Most package roots have a valid `current` and enter the existing menu directly.
If a discovered package has multiple manifests but no operational version, its
package screen must show the ambiguity and disable version-dependent mutation
rows. Aggregate health and update checks remain available because they target
each exact manifest rather than guessing a current version. The first release
need not add a version picker: an advanced user can invoke `gupkg <command>
<version path>` explicitly.

## Implementation boundaries

### New discovery domain

Add one focused module, for example `gupkg/discovery.py`, responsible for:

- collection traversal;
- package-root signature recognition;
- selector construction and resolution;
- inventory records and scan diagnostics.

Keep manifest parsing and validation in `configuration`, package path identity
in `layout`, update checks in `updates`, and UI rendering in `tui`. Discovery
may call those public domain functions but must not duplicate their rules.

The high-level facade coordinates:

1. parse command and target selection;
2. resolve package or collection mode;
3. build inventory when required;
4. dispatch one package operation or an aggregate read-only operation;
5. render one final result and exit code.

Do not change process working directory while iterating packages. Pass absolute
paths into existing workflows so package-local subprocess working directories
remain explicit and one package cannot affect the next.

### Rename mechanics

The rename must update:

- `src/pkg` to `src/gupkg`;
- absolute imports from `pkg...` to `gupkg...`;
- module docstrings, CLI descriptions, banners, and user-agent strings where
  they identify the application;
- tests that load `src/pkg/pkg.py` by file path so they exercise the installed
  package interface instead;
- launchers and documentation;
- dependency environment and private state paths;
- examples that show the old command.

Use `git mv` during implementation so file history remains readable. Do not
rename `pkg.toml`, `pkg.local`, hook protocol fields, or existing package
fixtures merely because they contain the package-format prefix.

### Public Python API

The supported programmatic surface is deliberately small:

- `gupkg.main(argv=None) -> int`, or the same function imported from the facade;
- the existing domain functions already used by repository-owned hooks/tests;
- a discovery function returning the inventory for callers that need it.

The console and module forms are primary. Do not promise every internal module
or helper as a stable library API, and do not add a service locator or plugin
interface to support the CLI.

## Safety and performance requirements

- Discovery is read-only.
- Health is read-only.
- Update availability does not activate or install anything.
- Selecting a package cannot broaden the collection root or escape it through
  `..`, a symlink, or a reparse point.
- Collection-wide mutation is not part of this release.
- Aggregate operations isolate failures and continue to the next package.
- Each physical package root is checked at most once per inventory.
- Manifest files are read once per health snapshot where practical.
- Output order is stable even when filesystem enumeration order changes.
- Depth truncation and inaccessible branches are visible; absence of a result
  must never be presented as proof that every package is healthy/current.
- The implementation must remain responsive for hundreds of package roots,
  without maintaining a daemon or persistent index.

## Documentation changes required during implementation

Update the long-lived documentation in the same implementation change:

1. Rename the README product title and all command examples to `gupkg`.
2. Add installation, `python -m gupkg`, and tool-installer examples.
3. Document package mode versus collection mode and the no-argument behavior.
4. Add the precise discovery signature, marker, max-depth, link, and conflict
   rules.
5. Show flat and nested collection layouts.
6. Document selectors, ambiguity errors, list filters, aggregate summaries, and
   exit behavior.
7. Update `docs/tui_style_guide.md` examples from `pkg tui` to `gupkg` and add
   the collection-to-package navigation rule without weakening its list-first
   style.
8. Update `docs/development_guide.md` with the packaging and discovery domain.
9. Update manual smoke instructions for console and module execution from an
   unrelated working directory.
10. Clearly list the intentionally retained package-format names (`pkg.toml`,
    `pkg.local`, and `PKG_MODULE_API`).

Old issue documents are historical records and do not need a mechanical
rename. Add a short architecture note only where an open issue would otherwise
direct new implementation to the obsolete `src/pkg` path.

## Test strategy

Follow `docs/tests.md`: protect observable behavior and real boundaries, not
private traversal helpers or the number and location of source modules.

### Installation and entry-point tests

Add integration coverage proving:

- a built wheel installs into an isolated environment;
- `gupkg --version` works outside the checkout;
- `python -m gupkg --version` reports the same version and exit code;
- console and module invocations preserve the caller's working directory;
- the TUI child-command builder uses the installed module rather than an
  adjacent source file;
- the deprecated repository `pkg.cmd`, if retained, delegates and warns.

These tests protect the reason for packaging: managed programs no longer need
a source copy.

### Discovery behavior tests

Use real temporary directory trees and protect these contracts:

- direct child package roots are discovered;
- arbitrary unmarked nested folders are ignored;
- an empty `gupkg-dir.toml` enables nested discovery;
- every grouping level in a deeper chain must be marked;
- malformed manifests remain visible and unhealthy;
- `current`, payload directories, and manager-state directories are not
  traversed;
- default max depth truncates a marked branch with a diagnostic;
- explicit smaller/larger depth values behave predictably;
- inaccessible or disappearing entries make the inventory incomplete without
  hiding successful siblings;
- reparse/symlink branches are not followed;
- a package/marker conflict is reported;
- results and diagnostics are deterministically ordered;
- configless package roots are ignored by discovery but remain usable by an
  explicit package path.

### Selection and aggregate CLI tests

Protect these user-visible behaviors:

- `--package name` selects a unique flat package;
- a canonical nested selector selects the intended duplicate basename;
- ambiguous short names list the valid selectors and return `2`;
- `--package` plus an explicit path is rejected;
- collection health checks every discovered manifest and continues after
  failures;
- update checks attempt every healthy manifest with update configuration,
  including historical versions and packages without `current`;
- a historical candidate not newer than the newest installed version is shown
  as superseded and does not make the package updatable;
- duplicate candidates are coalesced while conflicting candidates retain an
  actionable diagnostic;
- a package without update configuration is `not-configured`, not unhealthy;
- all/updatable/unhealthy filtering returns only concerned packages;
- updatable filtering performs fresh checks;
- empty collection operations succeed with explicit zero totals;
- aggregate exit status reflects partial and operational failures after all
  packages have been attempted;
- aggregate `--toml` output parses and contains stable package records.

Mock only real boundaries such as network responses, trusted hook execution,
Windows junction inspection where the test platform requires it, and
subprocesses. Do not mock private discovery call graphs.

### TUI behavior tests

Protect the observable navigation contract:

- package context opens the package home screen;
- collection context opens the collection home screen;
- All is the default list;
- Updatable performs checks before filtering;
- Unhealthy contains only concerned packages;
- selecting or clicking a package opens the same package action menu;
- Escape returns through package list to collection home;
- aggregate work does not block screen updates and completed counts refresh;
- ambiguous packages do not expose enabled mutation actions.

Exact widget trees, CSS strings, and internal screen class names are out of
scope for permanent tests.

### State migration tests

Protect:

- new update state is written to `.gupkg`;
- read-only operations can inspect legacy `.pkg` without renaming it;
- the first mutation migrates a lone legacy state directory;
- simultaneous `.pkg` and `.gupkg` directories stop mutation without deleting
  or merging user data;
- the legacy per-user virtual environment is never deleted or moved.

## Delivery phases

### Phase 1: installable rename

1. Add `pyproject.toml` and build/install smoke coverage.
2. Rename the import package and user-facing application to `gupkg`.
3. Add the console and `__main__` entry points backed by one dispatcher.
4. Change TUI child execution to `python -m gupkg`.
5. Migrate private state naming with the conflict policy above.
6. Add the temporary repository launcher shim and migration notes.

Exit criteria: an installed `gupkg` performs every current single-package
operation from an unrelated directory, and the old source wrapper is not
required.

### Phase 2: discovery and inventory

1. Add focused inventory records and signature-based traversal.
2. Add marked recursion, depth enforcement, link protection, deterministic
   selectors, and complete diagnostics.
3. Reuse existing layout and configuration logic for identity and health.

Exit criteria: read-only discovery produces a complete, deterministic
inventory for flat and marked nested collections without entering ignored
trees.

### Phase 3: aggregate CLI

1. Add target-mode resolution and `--root`, `--package`, and `--max-depth`.
2. Add `list` and its three filters.
3. Extend `config check` and `upgrade check` to collection mode.
4. Add aggregate rendering, TOML output, failure isolation, and exit reduction.
5. Require explicit selection for every mutating command in collection mode.

Exit criteria: all requested collection checks and selectors work
noninteractively with deterministic output.

### Phase 4: collection TUI

1. Add context-sensitive no-argument entry.
2. Add collection home, filtered package lists, aggregate result progress, and
   refresh behavior.
3. Reuse the package action screen after selection.
4. Apply the list-first style and ambiguous-version safety behavior.

Exit criteria: a keyboard or mouse user can check a collection, filter it, and
enter any concerned package's normal menu without editing a path.

### Phase 5: documentation and release cleanup

1. Complete the documentation changes listed above.
2. Run the full permanent suite plus wheel/install and Windows manual smoke
   checks.
3. Search active code and current documentation for obsolete product/import
   names, reviewing each retained package-format occurrence intentionally.
4. Announce the no-argument behavior and launcher/state migration.

## Non-goals

This change does not add:

- a central package registry or manifest database;
- background scans, automatic updates, or a daemon;
- unrestricted recursive `pkg.toml` search;
- discovery of configless packages;
- bulk install, download, activation, configuration rewrite, or legacy import;
- remote package repositories or dependency resolution between programs;
- marker-file configuration beyond presence;
- automatic migration of `pkg.toml`, `pkg.local`, or hook API names;
- permanent installed aliases named `pkg`;
- a generic provider, scanner, menu, or command framework.

## Acceptance criteria

The design is implemented only when all of the following are true:

1. A standard local install provides `gupkg` and `python -m gupkg` with one
   command contract.
2. The application works from directories that do not contain its source.
3. Product/import/state names use `gupkg`, while documented package-format
   compatibility names remain unchanged.
4. Bare `gupkg` opens the correct package or collection TUI from context.
5. Flat collection discovery finds direct package children and ignores
   irrelevant directories.
6. Empty `gupkg-dir.toml` files enable only the marked nested branches.
7. Discovery is depth-limited, link-safe, deterministic, and explicit about
   incomplete branches.
8. Every discovered manifest contributes to health status and diagnostics.
9. Update checks cover every eligible discovered manifest, preserve
   version-specific results, and compare candidates with the newest installed
   version before calling the package updatable.
10. `gupkg --package name` works for a unique package, while nested duplicates
    have deterministic selectors and actionable ambiguity errors.
11. CLI and TUI can show all, updatable, and unhealthy packages, with All as
    the default.
12. Selecting or clicking a package opens the same package menu used in direct
    package context.
13. Aggregate checks continue after individual failures, summarize all results,
    and return the defined exit category.
14. Collection mode never performs a bulk mutation or silently broadens its
    scan scope.
15. Legacy state migration never merges or deletes conflicting data.
16. User, developer, TUI, and smoke documentation describe the delivered
    behavior and migration.

## Final expected experience

A user installs `gupkg` once. From a normal package they run `gupkg` and see
that package's menu. From `D:\Programs` they run the same command and see a
plain collection list with health and update operations. Direct package
folders are found immediately; organization folders participate only by
placing an empty `gupkg-dir.toml` at each grouping level. The user filters to
unhealthy or updatable packages, selects one, and arrives at the familiar
single-package menu. Scripts use explicit commands and `--package` selectors,
and no managed program carries the manager's Python source.
