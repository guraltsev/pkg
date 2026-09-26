# Design: standalone self-hosting package and GitHub registry

Date: 2026-09-26
Priority: High
Change type: Distribution, bootstrap, manager configuration, registry, installation, and release engineering

## Summary

`gupkg` should become one of the packages it manages. A Windows release will
contain a self-contained Python runtime, the `gupkg` application, its runtime
dependencies, and a normal `pkg.toml`. The extracted `gupkg` package may live
anywhere. A small bootstrap entry point installs only its scope integration: a
native launcher at `C:\bin\gupkg.exe` for system scope or
`%USERPROFILE%\bin\gupkg.exe` for user scope, plus the corresponding `PATH`
entry.

The installed launcher must work from any current directory and must not need a
system Python, the source checkout, `pipx`, or `uv`. It launches the versioned
runtime in the active `gupkg` package wherever that package was extracted.
Configuration comes from the active version directory, a per-user roaming
override, or built-in defaults.

The same change adds an official read-only package registry hosted in the
GitHub repository. `gupkg registry sync` downloads and verifies an immutable
registry snapshot. Search and registry-backed installation use only the last
successfully verified local snapshot. Synchronization never executes registry
content. Installing a selected package copies its install seed into
the selected collection root and then delegates payload acquisition,
activation, and Windows integration to the existing single-package workflow.

This document is an implementation plan. The preliminary launcher phase is
implemented in the source tree; the standalone package, registry, and new
manager layout remain planned work.

## Goals

- Distribute `gupkg` as a standalone Windows package that needs no preinstalled
  Python.
- Keep `gupkg` as a normal versioned package at any user-selected filesystem
  location, with only its ignored local Python runtime allowed to be mutable.
- Install a native console shim at `C:\bin\gupkg.exe` for system scope or
  `%USERPROFILE%\bin\gupkg.exe` for user scope.
- Make the installed command independent of the caller's current directory.
- Default the system collection to `C:\opt` and the user collection to
  `%USERPROFILE%\opt`.
- Put collection roots, bin directories, and registry cache location in a
  strict TOML configuration.
- Download a verified official registry snapshot from GitHub.
- Search the cached registry and install a named package into an explicitly
  selected scope.
- Reuse the existing `pkg.toml`, bootstrap promotion, update, activation,
  component, manager, and TUI domains.
- Let the running `gupkg` safely update its own package and redirect future
  invocations to the new immutable version.
- Preserve an offline path through the last verified registry snapshot.

## Non-goals

- Supporting third-party or user-defined registries in the first release.
- Merging packages from multiple registries or defining registry priority.
- Building a package dependency solver.
- Running a daemon, scheduler, or automatic background synchronization.
- Automatically installing every package present in the registry.
- Moving existing installations into the new roots without an explicit
  migration command.
- Deleting old `gupkg` versions or an old shim during installation.
- Replacing the package directory, version directory, `current`, `pkg.toml`,
  or `.gupkg` models.
- Executing registry hooks while synchronizing, listing, or searching.
- Treating the Git working tree as the installed runtime.

## Decisions

### Preliminary launcher simplification

Before implementing the standalone package, registry, or new layout, simplify
the batch launchers and establish a strict ownership boundary between outer
package entry points, package-local bootstrap files, and Python.

The source layout after this preparation is:

```text
src\
  gupkg.cmd                         # external lightweight selector
  gupkg-tui.cmd                     # external lightweight TUI selector
  gupkg\
    gupkg.cmd                       # unavoidable local runtime bootstrap
    gupkg-tui.cmd                   # local TUI-to-Python delegation
    ... Python package ...
```

Packaged versions use the same relationship:

```text
v<version>.lN\
  gupkg.cmd
  gupkg-tui.cmd
  gupkg\
    gupkg.exe
    gupkg-tui.exe
    gupkg.cmd
    gupkg-tui.cmd
    python\...
    ... Python package ...
```

The two outer `.cmd` files are compatibility entry points, not bootstrap
programs. They may perform only these actions:

1. derive their own directory from `%~dp0`;
2. test whether the corresponding regular local executable exists;
3. execute that local executable when present, otherwise try the system-wide
   `gupkg.exe`; and
4. forward caller arguments and return the selected process's exit status.

For `gupkg.cmd`, the local candidate is
`%~dp0gupkg\gupkg.exe`; fallback is `gupkg.exe %*` from `PATH`.
For `gupkg-tui.cmd`, the local candidate is
`%~dp0gupkg\gupkg-tui.exe`; fallback is `gupkg.exe tui %*` from `PATH`.
The TUI wrapper does not prefer an unrelated system-wide `gupkg-tui.exe` over
the canonical installed command.

The external wrappers must not:

- search for or validate Python;
- download or extract Python or pip;
- generate `_pth`, `sitecustomize.py`, TOML, or configuration files;
- install Python or Textual dependencies;
- discover package or collection roots;
- add `--root`, rewrite commands, reorder caller arguments, or change the
  current directory;
- inspect manager configuration or registry state;
- perform elevation, update, install, or repair work;
- pause after execution; or
- fall back to another executable after a selected local executable starts and
  returns an error.

The only argument added by the TUI compatibility wrapper is the fixed `tui`
verb when it falls back to system `gupkg.exe`. Every caller-supplied token is
otherwise forwarded through `%*` in its original order. Executables are
invoked directly rather than through `call`.

The wrapper does not communicate its location through an environment variable
or an injected CLI option. The selected executable discovers package and
configuration context in Python from the caller's normal inputs and current
directory. Consequently, invoking a system fallback from outside a package
requires the same explicit package argument that a direct `gupkg.exe`
invocation would require.

### Internal batch boundary

Any batch logic that is genuinely required before Python can run moves from
the outer `src\gupkg.cmd` into `src\gupkg\gupkg.cmd`. This includes only the
minimum interpreter bootstrap boundary: selecting an explicitly configured or
healthy vendored interpreter, probing a supported system interpreter, and, if
none exists, invoking the pinned and verified embedded-Python bootstrap.

As soon as any Python interpreter can execute a script, control passes to a
Python bootstrap entry point. Python owns runtime validation, `_pth` and
`sitecustomize` generation, pip bootstrap after the interpreter exists,
dependency placement, package/config discovery, registry behavior, logging,
and final CLI dispatch. Static files should be copied from package resources
rather than emitted as long groups of batch `echo` statements.

`src\gupkg\gupkg-tui.cmd` contains no duplicate interpreter logic. It delegates
to its sibling internal `gupkg.cmd` with the fixed `tui` verb and forwards the
remaining arguments. If the packaged local executables are built and present,
ordinary users do not reach either internal batch file; they remain the source
checkout, recovery, and no-Python bootstrap boundary.

This preparation is behavior-preserving for package operations. It changes
which layer owns launch/bootstrap decisions before the standalone layout is
introduced.

### Preliminary implementation status

The launcher-only part of this design is intentionally complete, while the
standalone package and registry work remains unchecked below:

- [x] Add internal `gupkg\gupkg.cmd` and `gupkg\gupkg-tui.cmd` entry points.
- [x] Move no-Python interpreter selection and embedded-runtime download into
  the internal command file.
- [x] Hand runtime support-file generation, embedded pip setup, and final CLI
  dispatch to the Python bootstrap entry point.
- [x] Make the outer wrappers select package-local executables or a
  PATH-resolved system command and preserve arguments and exit status.
- [x] Rewrite automated launcher coverage around selection, forwarding,
  fallback, failure propagation, and the internal bootstrap handoff.
- [ ] Build and materialize package-local native `gupkg.exe` and
  `gupkg-tui.exe` shims.
- [ ] Perform the manual Windows coverage listed in the test strategy.
- [ ] Implement the standalone layout, manager configuration, GitHub registry,
  registry installation, and self-hosting phases.

### Default filesystem layout

The standalone manager package, package collections, command shims, and roaming
configuration are separate locations. No manager configuration file is placed
in `C:\opt` merely because it is the system collection root.

```text
D:\Tools\gupkg\                       # example; may be anywhere
  current\
  v0.13.0.l1\
    gupkg.cmd                         # thin local/system selector
    gupkg-tui.cmd                     # thin local/system TUI selector
    pkg.toml                          # standard package manifest
    gupkg-config.toml                 # optional version-local manager config
    gupkg\
      gupkg.exe                       # package-local native command
      gupkg.config.toml
      gupkg-tui.exe                   # package-local native TUI command
      gupkg-tui.config.toml
      gupkg.cmd                       # no-Python bootstrap/recovery boundary
      gupkg-tui.cmd                   # internal TUI delegation
      __init__.py
      __main__.py
      ...
      python\                         # generated/vendored local runtime
        python.exe
        python312._pth
        Lib\site-packages\...
  .gupkg\

C:\bin\
  gupkg.exe                           # system-scope shim
  gupkg.config.toml

C:\opt\                              # system package collection
  vscode\...
  vlc\...

%USERPROFILE%\bin\
  gupkg.exe                           # user-scope shim
  gupkg.config.toml

%USERPROFILE%\opt\                   # user package collection
  vscode\...

%APPDATA%\gupkg\
  gupkg-config.toml                  # optional per-user override
```

On Windows, `%APPDATA%` already expands to the current user's roaming data
directory, normally `C:\Users\<user>\AppData\Roaming`. The correct roaming
configuration path is therefore `%APPDATA%\gupkg\gupkg-config.toml`; it must
not add another `Roaming` path component.

### Installation scopes

The CLI uses the public words `user` and `system` for standalone and registry
operations. Internally they continue to map to `Scope.USER` and
`Scope.MACHINE`.

- User registry installs write packages beneath `%USERPROFILE%\opt`; self
  installation writes the shim beneath `%USERPROFILE%\bin`, updates
  `HKCU\Environment`, and never asks for elevation.
- System registry installs write packages beneath `C:\opt`; self installation
  writes the shim beneath `C:\bin`, updates the machine environment, and
  requires elevation before the first mutation.
- Registry-backed installation defaults to user scope. System installation is
  always explicit.
- A target selected from manager inventory retains its configured scope; a
  later `--scope` must not silently move it to the other root.

### Runtime and shim independence

The package location does not determine its integration scope. A user shim and
a system shim may point to the same arbitrary `gupkg` package or to different
package copies. Normal Windows `PATH` ordering decides which command is found
first. `gupkg self status` reports both detectable shims, their concrete
runtime targets, and the effective configuration source so this is visible
rather than implicit.

The bootstrapper does not relocate the running package and does not remove or
rewrite the other scope's shim.

## Manager configuration version 2

### File locations and discovery

Every standalone release may contain its default manager file at
`<gupkg-version>\gupkg-config.toml`. A user who wants settings to survive
unchanged across `gupkg` version updates places an override at
`%APPDATA%\gupkg\gupkg-config.toml`.

Configuration is selected in this order:

1. `--config PATH` selects one exact file.
2. `<cwd>\gupkg-config.toml` retains the existing explicit manager-directory
   behavior.
3. `%APPDATA%\gupkg\gupkg-config.toml` is the per-user roaming override when
   it exists.
4. The version-local file, `<active-version>\gupkg-config.toml`, is used when
   present.
5. Built-in schema defaults are used when none of those files exists.

A file found at a higher-precedence location that is malformed is an error; it
never silently falls through to a lower-precedence file. The native shim does
not hard-code or pass a manager config path. It only targets the standalone
runtime and forwards caller arguments.

The version-local config is copied forward as package support data during a
self-update, so a portable package can carry its own manager settings. The
roaming override is preferred for settings that should follow a Windows user
across package moves or parallel `gupkg` installations. A config initialization
command may write the built-in defaults to either supported location.

### Schema

The exact initial schema is:

```toml
mode = "manager"
schema_version = 2

[packages]
system = 'C:\opt'
user = '%USERPROFILE%\opt'

[bin]
system = 'C:\bin'
user = '%USERPROFILE%\bin'

[registry]
cache = '%LOCALAPPDATA%\gupkg\registry'
channel = "stable"
```

Version 2 accepts exactly these top-level keys and exact keys within each
table. `channel` initially accepts only `"stable"`. The official GitHub
repository, release asset names, signature algorithm, and trusted public keys
are application policy, not user-selectable registry endpoints. A future
schema can replace `[registry]` with multiple named registry definitions
without making arbitrary sources part of the first release contract.

`[bin]` is independent of `[packages]`. This lets `gupkg` live anywhere and
lets command placement change without moving either the manager package or an
application collection. The bundled config contains the shown defaults.

### Path processing and validation

Manager version 2 retains version 1 path rules:

1. Expand `%NAME%` against the case-insensitive process environment.
2. Expand a leading `~` to the current user's profile.
3. Resolve relative values against the directory containing the manager file.
4. Reject unresolved variables and shell expressions.
5. Normalize absolute paths for ownership and containment comparisons.

It adds these validations:

- package roots must be distinct and non-nesting;
- the registry cache must be outside both package roots;
- system and user bin directories must be distinct;
- an existing path must have the expected file or directory type;
- the selected config file must be a regular file; and
- configuration loading remains read-only.

Bootstrap may create the selected bin directory and registry cache. A
registry-backed package installation may create its selected collection root.
Ordinary configuration loading and discovery do not create any of them.

Changing a bin path does not silently delete the old shim. `gupkg self repair`
writes the shim to the new configured location and warns about the detectable
old location so the user can remove it explicitly.

### Installation context

`Scope` alone is no longer enough to install components. Introduce a small
installation context containing:

- selected scope;
- selected collection root;
- selected bin directory;
- effective manager configuration path; and
- shortcut root and registry environment boundary already used by components.

Manager commands construct this context from the validated manager config and
pass it through the existing single-package install workflow. Package-local
calls construct a compatibility context from the documented legacy defaults.
Do not let low-level component functions rediscover a manager file or inspect
the current directory.

The expansion context gains these install-time values:

- `$ScopeRoot`: selected collection root;
- `$Bin`: selected bin directory.

They are valid only in fields applied during installation. Configuration
selection stays a responsibility of the application entry point rather than a
package expansion variable.

`$VersionRoot` and `$Payload` are ordinary package variables available wherever
the existing `$App`, `$Icons`, and `$Shortcuts` variables are accepted.
`$VersionRoot` always means `<package-root>\current`; `$Payload` means its
configured lifecycle child; and `$App` remains `<package-root>\current\App`.

## The `gupkg` package

### Configurable lifecycle payload

Keep the standard manifest name `pkg.toml`. A second special manifest named
`gupkg.toml` would bypass current discovery, validation, registry publication,
and update behavior without adding useful information.

Add one optional canonical top-level field to `pkg.toml`:

```toml
payloadDirectory = "gupkg"
```

When absent, `payloadDirectory` defaults to `"App"`, preserving every existing
package. It names one safe immediate child directory of the version directory
that origin and update operations populate, validate, and replace.
It must be a nonempty relative name and must not contain separators, `.` or
`..`, use a reserved name such as `current`, `.gupkg`, `pkg.local`, `Icons`, or
`Shortcuts`, or resolve through a link/reparse point outside the version.

`$App` does not change meaning: it always resolves to
`<package-root>\current\App`. Add `$VersionRoot`, resolving to
`<package-root>\current`, for version-local siblings such as `gupkg`, `Icons`,
and `Shortcuts`. A spelling such as `$App\..\gupkg` may normalize to the same
place, but package definitions should use `$VersionRoot\gupkg` so they do not
depend on a physical `App` directory being present.

Add `$Payload`, resolving to
`<package-root>\current\<payloadDirectory>`, for generic code that genuinely
means the lifecycle payload. The `gupkg` package definition should still use
the more explicit `$VersionRoot\gupkg` spelling.

Built-in origin population, payload staging, and health checks operate on the
resolved `payloadDirectory`. Hook contexts add `PkgVars.Payload` and
`paths.stagePayload`. Existing `PkgVars.App`, `$App`, and `paths.stageApp`
continue to mean the literal `App` directory and retain their current behavior
when `payloadDirectory = "App"`; they are not silently rebound for a custom
payload. The existing `[update.check].appPath` remains a Git-check override;
its default becomes the configured `payloadDirectory`.

The installer's required-payload check also uses `payloadDirectory`. Therefore
the `gupkg` package is valid with a populated `gupkg` directory and no `App`
entry at all; the absent literal `App` is not treated as damage.

The normalized runtime configuration owns this path choice. Do not put it on
the directory-derived `PackageIdentity` and do not add a `gupkg`-specific
branch to the installer.

### Runtime payload

The Windows payload should use the official embeddable CPython distribution,
not a one-file freezer. A directory runtime is still standalone and better
preserves the current trusted hook and optional dependency model.

```text
gupkg\v0.13.0.l1\
  gupkg.cmd
  gupkg-tui.cmd
  pkg.toml
  gupkg-config.toml
  gupkg\
    gupkg.exe
    gupkg.config.toml
    gupkg-tui.exe
    gupkg-tui.config.toml
    gupkg.cmd
    gupkg-tui.cmd
    __init__.py
    __main__.py
    core.py
    ...
    python\
      python.exe
      python3.dll
      python312.dll
      python312.zip
      python312._pth
      Lib\site-packages\
```

The `gupkg` directory is both the Python application package and the lifecycle
payload selected by `payloadDirectory`. Its `python` child is intentionally
ignored in the source/registry working tree and materialized by the release
build or the internal package-local bootstrap command. The release ZIP
includes a complete runtime so first installation needs no system Python.

`python312._pth` admits the bundled standard library, bundled site-packages,
and the version directory so `import gupkg` resolves the sibling application
package. It enables `sitecustomize` solely to add the documented mutable
dependency directory. The release includes all ordinary runtime dependencies,
including Textual, and includes `pip` only for the existing explicit
package-hook dependency installation workflow. No import path points at the
source checkout.

The local `gupkg\python` directory is the one deliberate mutable exception
inside this package version. Package hashes, registry publication, and support
tree copying exclude it; release-asset hashes still cover the materialized
runtime shipped to users. Manager state, registry cache, update work, and
optional hook dependencies remain in their established external locations.

The release build must be reproducible from a locked dependency set, record
the CPython archive digest, and produce a software bill of materials plus
SHA-256 digests for release assets.

### Package-local native launchers

`gupkg\gupkg.exe` and `gupkg\gupkg-tui.exe` are copies of the existing native
console shim. Their adjacent configuration files resolve relative paths from
the directory containing the shim, so they remain relocatable with the
package. Their effective configurations are:

```toml
# gupkg\gupkg.config.toml
target = "python\\python.exe"
forward_arguments = true
elevate = false

[[argument]]
value = "-m"

[[argument]]
value = "gupkg"
```

```toml
# gupkg\gupkg-tui.config.toml
target = "python\\python.exe"
forward_arguments = true
elevate = false

[[argument]]
value = "-m"

[[argument]]
value = "gupkg"

[[argument]]
value = "tui"
```

Neither native launcher fixes a working directory. Both preserve the caller's
current directory, prepend only their declared fixed arguments, forward the
caller's remaining arguments, and return the Python process's exit status.
The outer command files neither read nor synthesize these TOML files.

### Package definition

The official registry contains `gupkg/vbootstrap.l1/pkg.toml`. Its GitHub
release check selects the standalone Windows runtime asset and the normal
bootstrap promotion flow creates a concrete immutable version. The concrete
bootstrap archive distributed to first-time users contains the same effective
manifest and a populated `gupkg` payload, including its local Python runtime.

The important component declaration is equivalent to:

```toml
payloadDirectory = "gupkg"

[[environment]]
Name = "GUPKG_HOME"
Value = "$VersionRoot\\gupkg"

[[bin]]
name = "gupkg"
target = "$VersionRoot\\gupkg\\python\\python.exe"
arguments = ["-m", "gupkg"]
type = "console"
forward_args = true
```

`GUPKG_HOME` is an optional convenience pointing through `current` to the
active version's `gupkg` support directory; configuration discovery does not depend on it.
Do not set global `PYTHONHOME` or `PYTHONPATH` values for the embedded runtime.

No `working_dir` is set. The child inherits the caller's current directory so
relative explicit package paths and ad-hoc collection behavior remain useful.

The installed command consists of two files:

- `gupkg.exe`, copied from the versioned native console shim resource; and
- `gupkg.config.toml`, containing the concrete runtime path and launcher
  behavior.

The shim target may point to the concrete immutable version rather than the
`current` junction. Activating a new `gupkg` version then rewrites only the
small shim configuration for future invocations. The old process continues
from its already loaded runtime, and old versions remain recoverable.

### Self-update and the running shim

The native shim is the parent process while `gupkg` runs, so Windows may deny
replacement of `gupkg.exe` during self-update. Normal releases therefore keep
the launcher protocol binary stable and compare existing bytes before writing;
an identical running shim is left untouched while its adjacent configuration
is atomically redirected to the new runtime.

If a release requires new shim bytes, the installer writes
`gupkg.exe.pending` and starts a versioned replacement helper. The helper waits
for both the child runtime and parent shim to exit, verifies the pending file's
digest, atomically replaces the shim, and records the outcome. Until that
replacement succeeds, the compatible old shim and newly written configuration
remain usable. A shim protocol bump that is not backward compatible requires a
bootstrap release and must not be attempted as an ordinary self-update.

### Bootstrap artifact

Each Windows release publishes `gupkg-bootstrap-windows-x64.zip` containing:

```text
gupkg-bootstrap.exe
gupkg-bootstrap.config.toml
payload\gupkg\v<version>.l1\gupkg\...
payload\gupkg\v<version>.l1\gupkg-config.toml
payload\gupkg\v<version>.l1\pkg.toml
```

The bootstrap executable is the same audited native console shim pointed at
the included runtime with a fixed `self install` argument. It supports:

```text
gupkg-bootstrap.exe --scope user
gupkg-bootstrap.exe --scope system
```

The archive must be extracted before execution. Bootstrap refuses to run from
inside the ZIP shell namespace or from a partial payload.

The first installation never fetches `gupkg` from the registry: the concrete
package bundled in the bootstrap archive is the seed of trust and removes the
otherwise circular dependency on an already installed package manager. The
official registry's `gupkg` entry is used for later discovery, repair, and
normal package updates.

Bootstrap performs these steps:

1. Validate the bundled package layout and release manifest before mutation.
2. Resolve the requested scope and request elevation once, before any system
   mutation.
3. Resolve the package root inside the extracted archive without relocating
   it; the user-selected extraction directory remains the installation
   location.
4. Strictly validate the version-local `gupkg-config.toml`, roaming override,
   or built-in defaults selected by normal precedence.
5. Run the normal install workflow against that package root, creating or
   repairing `current`, the selected scope's `gupkg.exe`, and its shim config.
6. Ensure `C:\bin` or `%USERPROFILE%\bin`, as selected, is on the corresponding
   `PATH`.
7. Synchronize the official registry.
8. Print the installed command, effective config source, package root,
   registry revision, and whether a new terminal is needed to observe the
   `PATH` change.

If registry synchronization fails after the command is installed, bootstrap
returns a partial-success mutation status and explains that `gupkg registry
sync` can be retried. It does not roll back a working local command because the
registry is not required to manage already installed packages.

Re-running bootstrap is a repair operation. Matching package versions and shim
files are reused, missing pieces are restored, and conflicting bytes stop with
an actionable error rather than being overwritten.

## Corner cases and required behavior

### Lightweight launcher selection

- A directory named `gupkg.exe` or `gupkg-tui.exe` is not a local executable.
  The wrapper must distinguish a regular file from a directory before selecting
  it.
- Once a regular local executable is selected, access denied, bad-image,
  antivirus quarantine, startup failure, and any nonzero child result are
  returned directly. Falling back after such a failure could run a different
  manager version against the same package and is forbidden.
- System fallback is resolved from `PATH`, not from an unrelated executable in
  the caller's current directory. The batch implementation should use
  `where.exe`'s explicit `$PATH:...` search form or an equivalently narrow
  mechanism, then invoke the resolved absolute path.
- If no system executable is found, the wrapper prints one concise diagnostic
  naming the missing local candidate and `gupkg.exe` fallback, then returns a
  nonzero launcher error without downloading or repairing anything.
- `%~dp0`, the local executable path, and the resolved system path are always
  quoted. Package locations containing spaces, `&`, parentheses, Unicode, or a
  UNC prefix must not cause the wrapper to change directories or reconstruct a
  command string.
- `DisableDelayedExpansion` remains enabled so exclamation marks in forwarded
  arguments are not consumed by the wrapper. The wrapper provides ordinary
  Windows batch `%*` forwarding guarantees; callers needing arguments that
  cannot survive `cmd.exe` parsing should invoke `gupkg.exe` directly.
- An outer wrapper invoked through a relative path, absolute path, symlink, or
  junction uses `%~dp0` only to locate its sibling `gupkg` directory. It does
  not infer or pass package identity. Duplicate physical roots are handled by
  normal Python layout validation.
- `gupkg-tui.cmd` always owns the TUI intent. On system fallback it prepends
  exactly one `tui` verb and performs no attempt to interpret whether later
  arguments resemble another command.
- Neither wrapper uses `pushd`/`popd`. The selected process sees the caller's
  original working directory and applies the normal Python discovery rules.

### Manifest and payload identity

- `pkg.toml` remains the only package manifest. A neighboring `gupkg.toml` is
  not an alias and is ignored by package discovery.
- `payloadDirectory` is case-preserving but compared case-insensitively on Windows.
  `App` and `app` therefore identify the same physical name and cannot coexist
  as distinct payloads.
- The value must be one immediate child name. Absolute paths, drive-qualified
  paths, separators, alternate data stream syntax, trailing dots/spaces,
  device names, `.`/`..`, and reserved package directories are rejected.
- A payload entry that is a symlink, junction, or other reparse point is
  rejected before origin refresh, update staging, or deletion. No payload
  operation may escape the concrete version directory.
- `payloadDirectory` is part of the package's update contract. A staged version
  may change it only when the candidate manifest explicitly changes it and
  supplies a complete payload at the new location. The updater never renames
  the old version's payload in place.
- `[update.check].appPath` does not redefine the install payload. It selects a
  check location only; confusing values receive diagnostics that name both the
  check path and configured payload directory.
- Module hooks for a custom payload use `PkgVars.Payload` and
  `paths.stagePayload`. Reading or writing the legacy `App`/`stageApp` keys
  still addresses the literal `App` directory and cannot accidentally replace
  the sibling `gupkg` payload.

### Local Python runtime

- Git and registry source snapshots omit `gupkg\python`, but the published
  standalone/bootstrap ZIP must contain a complete verified runtime. A release
  with a missing or partial runtime is invalid.
- Developer or package-local copies without `gupkg\python` use the version's
  `gupkg.cmd` bootstrap path. It downloads into a unique sibling temporary
  directory, verifies the pinned archive digest, configures `_pth` and pip,
  runs an import/version smoke check, and only then renames the directory to
  `python`.
- A failed, interrupted, or out-of-space bootstrap never leaves a partial
  directory named `python`. A later run may remove stale bootstrap work after
  confirming that no recorded process owns it.
- An existing runtime is accepted only when its architecture, supported Python
  version, expected standard-library files, `_pth` policy, and ability to
  import `gupkg` all pass. A directory that merely contains `python.exe` is
  insufficient.
- The installed native shim normally targets the concrete
  `gupkg\python\python.exe`. If that runtime is later deleted or quarantined,
  the shim cannot repair itself. Its error text and `self status` documentation
  direct the user to run the version-local `gupkg.cmd` or re-run the release
  bootstrap, which repairs the runtime before repairing the shim.
- A read-only package location works only when the release already contains a
  healthy runtime. Bootstrapping or repairing `gupkg\python` requires write
  access to the concrete version and fails before changing shims when that
  access is unavailable.
- Optional hook dependencies are not installed into `gupkg\python`; they
  remain in the existing per-user dependency directory. This prevents package
  hooks from silently changing the vendored interpreter.
- `gupkg\python` is excluded from ordinary support-tree copy-forward. Each new
  release payload must provide or bootstrap its own validated runtime so an old
  interpreter is not accidentally paired with incompatible new code.

### Python import and process behavior

- The embedded `_pth` file adds the concrete version directory, not the
  `gupkg` package directory, so `python -m gupkg` imports
  `<version>\gupkg\__main__.py` rather than searching the caller's directory.
- The caller's working directory is not added ahead of the packaged
  application. A malicious or accidental `gupkg.py` in the current directory
  must not shadow the installed package.
- Environment variables such as `PYTHONHOME`, `PYTHONPATH`, and an active
  virtual environment do not redirect the embedded interpreter. Explicitly
  supported package-hook dependency paths are added after the packaged
  application path.
- The `gupkg\python` child must not become a supported import namespace such
  as `gupkg.python`; no public code or hooks may depend on importing it.
- Console control events, Unicode arguments, spaces, and trailing backslashes
  must survive the native shim to embedded Python without a `cmd.exe` parsing
  layer during normal operation.

### Configuration selection and persistence

- An absent version-local config is valid and uses built-in defaults unless a
  higher-precedence config exists.
- An empty, malformed, unreadable, directory-valued, or unsupported-schema
  config at a higher-precedence location is an error. It does not fall through
  to a lower-precedence default.
- A system shim still resolves `%APPDATA%` for the user who invokes it. It does
  not create or reuse another administrator's roaming config after elevation.
  Values are resolved before elevation and passed to the elevated continuation
  for revalidation.
- Self-update copies the exact version-local `gupkg-config.toml` forward before
  activation. A roaming override is never copied into a package version and is
  never modified by update.
- If both version-local and roaming configs exist, `self status` names the
  roaming file as effective and reports the shadowed version-local file.
- Moving the whole package preserves its version-local config but invalidates
  absolute shim targets. `self repair` from the new location rewrites the
  selected shim; it does not search disks for moved packages.

### Self-update, parallel copies, and locks

- `gupkg self update` targets the package root containing the running module,
  not whichever package named `gupkg` happens to appear in `C:\opt`,
  `%USERPROFILE%\opt`, the registry, or earlier on `PATH`.
- `gupkg install gupkg` from the registry may create a separate managed copy.
  The command must warn when another installed shim currently targets a
  different physical package root.
- User and system shims may target the same package. Updating through either
  scope activates one new version, then repairs only the requested scope's
  integration unless `self repair --scope all` is explicit.
- Two concurrent self-updates serialize on the package-root update lock. A
  second process reports the owning PID/state and performs no staging or shim
  changes.
- The running interpreter and parent shim remain in the old version until the
  command exits. Activation changes `current` and future shim configuration;
  it never overwrites the running code or runtime.
- A broken or escaping `current` junction does not prevent a concrete-version
  shim from starting. `self status` reports the conflict and `self repair`
  recreates `current` only after verifying the concrete target belongs to the
  same package root.
- Old-version cleanup must skip the running version, every version referenced
  by a detectable shim, versions with active locks, and the rollback version.

### Registry and release contents

- The registry seed may contain the `gupkg` source/support directory but must
  omit its ignored `python` runtime. The separately published bootstrap ZIP and
  runtime update asset contain that runtime and are covered by their release
  digests.
- Registry hashing distinguishes declared support files from the ignored
  runtime path. An unexpected executable under a registry seed's
  `gupkg\python` is a publication error rather than silently ignored content.
- Safe extraction applies before recognizing `payloadDirectory`; an archive cannot
  use a crafted payload name, case collision, link, or reparse point to replace
  `pkg.toml`, `gupkg-config.toml`, another version, or manager state.
- Antivirus quarantine, sharing violations, and delayed-delete behavior are
  reported separately from checksum or configuration failures so repair advice
  is actionable.
- A successful local self-install is not rolled back when registry sync fails.
  Conversely, a valid registry snapshot does not make a broken local Python
  runtime healthy.

## Official GitHub registry

### Publication

The official source is the GitHub repository already associated with this
project, `https://github.com/guraltsev/pkg`. A release workflow builds a
deterministic registry snapshot from `pkgs/` and publishes three assets:

- `gupkg-registry-v1.toml`: signed snapshot metadata and package index;
- `gupkg-registry-v1.toml.sig`: detached signature; and
- `gupkg-registry-v1.zip`: package definition trees.

Clients use stable `releases/latest/download/...` URLs for the `stable`
channel. Development builds may expose an explicit internal option for a local
snapshot, but branch archives and arbitrary URLs are not part of the public
version-one contract.

Publishing fails if registry validation fails. A Git tag or GitHub release by
itself is not sufficient publication.

### Registry index

The signed index contains data, not executable configuration:

```toml
schema_version = 1
registry = "official"
revision = "<full git commit>"
generated_at = "2026-09-26T00:00:00Z"
archive = "gupkg-registry-v1.zip"
archive_size = 123456
archive_sha256 = "<sha256>"

[[package]]
selector = "gupkg"
path = "packages/gupkg"
description = "The gupkg package manager"
manifest_sha256 = "<sha256>"
```

Package rows are sorted by case-folded selector and then original selector.
Selectors are unique case-insensitively. Version one accepts flat selectors
only; nested registry selectors can be designed with multi-registry support.

Every package path contains exactly one install seed version tree plus optional
package-owned support files used by that seed. A seed may be a
`vbootstrap*.lN` update template or a concrete `v<version>.lN` definition whose
missing declared payload can be populated from `[origin]`. This preserves the package
shapes already present under `pkgs/` without copying application payloads into
the registry. Registry publication rejects:

- `current`, `.gupkg`, the declared payload directory, or manager state,
  except for explicitly allowed source/support files in the `gupkg` seed;
- absolute paths, `..`, symlinks, junctions, and reparse points;
- manifests whose directory-owned identity is inconsistent;
- duplicate selectors or case collisions;
- unknown registry schema fields;
- files omitted from the snapshot hash inventory; and
- package trees that fail the existing configuration health checks.

The registry is a catalog of install definitions, not a mirror of application
payloads. Payloads continue to come from each package's declared origin or
update provider.

### Trust and verification

Registry definitions can contain trusted `pkg.local` code that later runs
during installation or updates. HTTPS and a checksum served beside an archive
are not a sufficient trust boundary by themselves.

The client bundles the official registry's public signing key. Synchronization
must:

1. download the index and detached signature with bounded sizes and timeouts;
2. verify the signature before trusting URLs, sizes, hashes, or package rows;
3. download the named archive with a configured maximum size;
4. verify its exact byte count and SHA-256 digest;
5. extract into a new temporary directory with traversal, link, reparse-point,
   file-count, and expanded-size protections;
6. verify every indexed package manifest and reject unexpected files;
7. validate every package definition without importing or executing hooks; and
8. atomically publish the snapshot as current only after all checks succeed.

Key rotation is delivered by a `gupkg` release that trusts both the retiring
and replacement keys for an overlap period. A registry snapshot cannot add a
new trusted key by itself.

### Local cache

The default cache is:

```text
%LOCALAPPDATA%\gupkg\registry\official\
  downloads\
  snapshots\<revision>\
    registry.toml
    packages\...
  state.toml
```

`state.toml` identifies the active verified revision, source ETag when
available, successful synchronization time, and diagnostic state. It is
atomically replaced. A failed sync leaves the previous snapshot active and
removes disposable work when possible.

Only the cache for the invoking user is updated, even when installing a system
package. Elevation is delayed until the package-root mutation, so ordinary
registry downloads and searches do not require administrator privileges.
The elevated continuation revalidates the selected snapshot revision and
package hashes before copying anything into `C:\opt`.

Keep the newest two verified snapshots so an interrupted reader can finish;
older cache cleanup is explicit and recoverable. Cache contents are never
treated as installed package state.

### Synchronization behavior

`gupkg registry sync` is the only ordinary command that refreshes a populated
cache. It uses HTTP conditional requests when possible and reports `current`
without replacing the snapshot when GitHub returns an unchanged result.

If no verified cache exists, commands that inherently need registry data may
offer or perform one foreground synchronization:

- bootstrap always attempts it;
- `gupkg install <selector>` synchronizes when the cache is absent;
- `gupkg search` synchronizes when the cache is absent; and
- local list, doctor, health, and update commands never contact the registry.

With an existing cache, install and search do not silently refresh it. They
show the snapshot revision and age, and the TUI offers `Sync registry` as an
explicit action. `--offline` forbids network access and fails clearly when no
verified snapshot exists.

## Registry-backed package installation

### Command surface

Add these commands:

```text
gupkg registry sync [--offline]
gupkg registry status
gupkg search [QUERY] [--installed|--available]
gupkg install SELECTOR [--scope user|system] [--offline]
gupkg self status
gupkg self repair [--scope user|system]
gupkg self update [--scope user|system]
```

Existing local-path installation remains supported:

```text
gupkg install .
gupkg install C:\staging\tool\v1.2.3.l1
```

Install argument classification is deterministic:

1. an existing filesystem entry is a local path;
2. an absolute path, drive-qualified path, `.`/`..`, or value containing a path
   separator is path syntax and remains a local-path error when missing;
3. any other token in manager mode is a registry selector; and
4. a bare selector outside manager mode is rejected with guidance to use an
   installed manager config or `--config`.

This avoids making a misspelled absolute path into a remote package request.
Registry selectors never come from manifest display names.

`gupkg self update` is a convenience spelling that selects the `gupkg` target
in the requested scope and delegates to the normal check, download, and install
workflow. It does not use a separate updater.

### Installation transaction

Installing a registry selector performs these steps:

1. Load and validate manager configuration and the selected scope layout.
2. Load one verified cached registry snapshot, synchronizing only when absent
   and not offline.
3. Resolve the selector case-insensitively to exactly one indexed package.
4. Validate scope permissions and obtain elevation before the first system
   write.
5. Acquire a collection mutation lock under
   `<scope-root>\.gupkg\locks\install.lock`.
6. Revalidate the registry revision and all selected package hashes.
7. Copy the install seed into a unique work directory beneath
   `<scope-root>\.gupkg\work`.
8. Run the ordinary manifest and health validation against the staged tree.
9. Atomically create the seed's declared version directory beneath
   `<scope-root>\<selector>` without replacing an existing conflicting
   directory.
10. Call the existing `install_package(...)` workflow with the exact manager
    installation context. Bootstrap promotion downloads a concrete payload,
    commits its immutable version, activates `current`, and installs declared
    components.
11. Release the lock and report the registry revision, installed version,
    scope, and resulting command or shortcut paths.

If payload download or installation fails after the install seed is committed,
keep that definition as an uninstalled, repairable package. A retry
uses the same package-local update lock and established repair behavior. Never
leave a partially copied version directory at its final name.

If the target package already exists:

- identical seed content is reused;
- additional immutable registry versions may be added only when their final
  path is absent;
- a byte conflict at an existing version path is an error;
- installed versions, `current`, and `.gupkg` are never replaced from registry
  cache; and
- the registry does not become authoritative for local installation state.

### Inventory and TUI integration

Manager inventory remains filesystem-authoritative for installed state. A
separate catalog view joins cached registry metadata by selector for display
and installation choices; it does not synthesize package directories during
list or search.

The manager TUI adds:

- `Search packages`;
- `Sync registry` with current revision and age;
- an Available filter that includes catalog-only packages; and
- an Install action that requires a visible User/System choice, defaulting to
  User.

Installed package rows retain their current health and update semantics.
Registry availability is an additional fact, not an installed status. Opening
an installed package continues to use the existing per-package screen.

## Architecture changes

### `bootstrap` domain

Add a dependency-free Python bootstrap entry point that can be executed by
absolute script path as soon as either a supported system interpreter or the
fresh embedded interpreter exists. It owns runtime inspection, generated-file
installation from packaged templates, pip initialization, dependency paths,
and the handoff to `gupkg.main(...)`.

The internal `gupkg\gupkg.cmd` owns only the earlier boundary where no Python
can yet run. The outer command files never call bootstrap operations directly.
The bootstrap module does not own package discovery, manager configuration,
registry synchronization, or normal command parsing; it prepares a valid
interpreter and delegates those behaviors to their existing Python domains.

### `distribution` domain

Add a focused module responsible for self-install and self-inspection:

- identify the running packaged version and reject a checkout-only runtime;
- resolve and validate the packaged default or roaming manager configuration;
- preserve the bundled package at its user-selected location;
- construct the installation context;
- coordinate normal installation of the `gupkg` package; and
- report/repair installed shims without owning generic component logic.

It must not implement a second manifest parser, updater, shim writer, or
activation mechanism.

### `registry` domain

Add a focused module responsible for:

- official endpoint policy;
- index parsing and signature verification;
- bounded HTTP downloads;
- safe snapshot extraction and hash validation;
- atomic cache publication;
- catalog search and exact selector resolution; and
- staging one selected definition for the normal installer.

It must not import `pkg.local`, check application updates, activate versions,
write Windows integrations, or infer installed state.

### Existing domains

- `manager` owns the version 2 configuration and produces scope installation
  contexts.
- `collection` remains responsible only for local package discovery.
- `layout` resolves/activates package versions but no longer hard-codes manager
  bin roots.
- `configuration` validates `payloadDirectory` and exposes one resolved
  lifecycle payload path while preserving `App` as the default.
- `origin` populates and replaces the resolved payload directory rather than a
  hard-coded `App` child.
- `components` consumes the supplied installation context and continues to
  own shim and `PATH` installation.
- `updates` continues to promote registry bootstrap definitions into concrete
  version directories and excludes the declared payload plus ignored local
  runtime state when copying support files.
- the CLI facade coordinates registry selection and delegates one selected
  package to existing workflows.
- the manager TUI calls the same registry sync/search/install boundaries as
  the CLI.

No global service locator or provider framework is needed.

## Failure and recovery rules

- A malformed manager config stops before network or filesystem mutation.
- A failed or untrusted registry sync never replaces the active cache.
- An absent registry cache does not block local package management.
- A stale verified cache remains usable and is labeled with its age/revision.
- A package install never broadens from user to system scope.
- Elevation cancellation leaves both roots unchanged.
- An install seed committed before a payload failure remains visible as
  uninstalled and repairable.
- A self-update never deletes the old runtime version.
- A running compatible shim is not overwritten.
- A pending shim replacement is reported by `self status` and retried by
  `self repair`.
- Changing config paths never silently moves packages or deletes old shims.
- Registry cache cleanup never touches package roots.

## Compatibility and migration

Manager schema version 1 remains readable. It derives each bin directory from
the legacy component defaults so existing installations do not silently move
their commands. `gupkg manager migrate-config` writes a reviewed version 2
file; loading alone never edits it.

The repository `src\gupkg.cmd` and `src\gupkg-tui.cmd` remain only as thin
development and compatibility selectors. Source/runtime bootstrap behavior
lives under `src\gupkg\`, and the Python console entry point and
`python -m gupkg` remain supported. The standalone native shim becomes the
documented end-user installation.

Existing package roots outside `C:\opt` and `%USERPROFILE%\opt` remain usable
through explicit paths or existing manager files. This design changes defaults
for new bootstraps, not the validity of old layouts.

## Test strategy

Follow `docs/tests.md`: protect observable contracts and real boundaries, not
private helper call graphs.

### Launcher-boundary tests

Replace the current broad outer-launcher bootstrap expectations with observable
selection and forwarding coverage:

- `gupkg.cmd` selects a regular package-local `gupkg\gupkg.exe` before any
  system command;
- `gupkg-tui.cmd` selects package-local `gupkg\gupkg-tui.exe` and otherwise
  invokes the resolved system `gupkg.exe` with one leading `tui` verb;
- absence of a local executable falls back only to an executable found through
  `PATH` and not an unrelated current-directory executable;
- a directory using an `.exe` name is not selected;
- a selected local executable's nonzero status is returned without fallback;
- a missing local and system executable produces the documented diagnostic and
  nonzero launcher status;
- spaces, Unicode, exclamation marks, empty quoted values, and ordinary quoted
  argument groups reach a probe executable with their supported batch meaning;
- neither wrapper injects package/config arguments, changes the current
  directory, or exports package-context environment variables;
- invoking a wrapper from an unrelated working directory does not change that
  directory in the child; and
- outer wrappers cause no network, runtime-bootstrap, configuration-write, or
  dependency-install side effects.

Test internal bootstrap behavior separately at its real boundaries: supported
interpreter selection, verified embedded-runtime installation, atomic failure
cleanup, and handoff to the Python bootstrap entry point. Do not assert exact
batch labels, line order, or implementation text.

### Configuration tests

Protect these behaviors with real TOML files and temporary directories:

- schema version 2 accepts the documented default layout;
- environment and relative paths resolve against the manager file;
- unknown tables/keys, unresolved variables, nested roots, invalid bin paths,
  and cache paths inside package roots fail before mutation;
- schema version 1 remains readable with legacy bin behavior;
- manager-derived installation context reaches component placement; and
- explicit config, current-directory config, roaming config, and version-local
  config follow the documented precedence;
- `%APPDATA%` is used directly without appending another `Roaming` component;
  and
- a malformed higher-precedence config never falls through to a lower one.

### Registry tests

Use a local HTTP test server or mocked HTTP boundary and real archives:

- valid signed metadata and matching archive publish one snapshot;
- bad signature, wrong size/hash, unsupported schema, duplicate selector,
  unsafe path, symlink/reparse entry, excessive expansion, unexpected file, or
  invalid package leaves the old snapshot active;
- conditional synchronization preserves the current snapshot on no change;
- a failed first sync leaves no active cache;
- offline search/install uses a verified cache and never calls the network;
- search ordering and exact selector resolution are deterministic;
- synchronization never imports or executes a registry hook; and
- cache state reports revision, age, source, and last failure accurately.

### Registry installation tests

Protect these user-visible behaviors:

- a bare manager-mode selector installs into the user root by default;
- explicit system scope is resolved and elevation is requested before writes;
- path-looking arguments never fall through to registry selection;
- staged definitions are validated before final placement;
- `payloadDirectory` defaults to `App` and a safe `gupkg` value drives origin,
  update, and health behavior without changing `$App`;
- `$VersionRoot` targets version-local support such as `gupkg`, while `$App`
  continues to mean only the `App` sibling;
- hook contexts expose distinct payload and App paths, with legacy hooks
  unchanged for default packages;
- unsafe, reserved, linked, or case-colliding payload directory names fail
  before mutation;
- identical install seeds are idempotent;
- conflicting immutable paths stop without overwrite;
- payload failure leaves a repairable uninstalled seed;
- retry delegates to the established package bootstrap workflow;
- installed state is read from the filesystem, not the registry index; and
- concurrent collection installs are serialized.

### Standalone and self-hosting tests

Build the release artifact in CI and test it in a clean Windows environment
with no discoverable Python:

- the bundled runtime starts `gupkg --version` from an unrelated directory;
- user bootstrap leaves the package in its extracted location and creates the
  current junction, `%USERPROFILE%\bin` shim, shim config, and user `PATH`
  entry;
- system bootstrap requests elevation once and writes only system-owned state;
- re-running bootstrap repairs missing files without damaging matching state;
- the installed shim passes arguments and exit codes unchanged;
- the shim does not embed a manager config path and the runtime resolves the
  documented config precedence;
- the optional version-local config is copied forward and a roaming config survives a
  self-update;
- missing, partial, wrong-architecture, and quarantined local Python runtimes
  produce the documented repair behavior;
- the embedded runtime ignores caller Python path and virtual-environment
  redirection;
- self-update activates a new versioned runtime and redirects a later command;
- an identical running shim is not rewritten;
- a changed compatible shim is replaced after the parent exits;
- old runtime versions remain runnable for recovery; and
- registry failure after local installation reports partial success while the
  installed command remains usable.

### Manual Windows coverage

Add smoke cases for UAC acceptance/cancellation, PATH visibility in a new
terminal, user/system coexistence, antivirus/file-lock interference, locked
shim replacement, offline first run, proxy/TLS failure, interrupted downloads,
long paths, and a home directory containing spaces and non-ASCII characters.

## Delivery phases

### Phase 0: thin launchers and Python-owned bootstrap

- [x] Add internal `gupkg\gupkg.cmd` and `gupkg\gupkg-tui.cmd` entry points.
- [x] Move unavoidable no-Python interpreter discovery/download work into the
  internal command file.
- [x] Add a Python bootstrap entry point and move `_pth`, site customization, pip,
  dependency, path, configuration, and dispatch logic into Python as soon as
  an interpreter is runnable.
- [ ] Materialize package-local native `gupkg.exe` and `gupkg-tui.exe` shims with
  relative configurations targeting the vendored interpreter.
- [x] Replace the external `gupkg.cmd` and `gupkg-tui.cmd` with local-executable
  selectors plus PATH-only system fallback and exact argument/exit forwarding.
- [x] Rewrite launcher tests around the observable boundary.
- [ ] Perform manual Windows checks for quoting, PATH resolution, UNC paths,
  and missing tools.

Exit criteria: the two external wrappers contain no package-manager or runtime
bootstrap policy; they only select a local or installed executable, forward
arguments, and return its result. All behavior that can run after Python starts
is implemented in Python.

### Phase 1: configuration-owned installation locations

1. Add manager schema version 2 and migration output.
2. Introduce installation context and remove manager-mode reliance on hard-coded
   bin paths.
3. Add `$ScopeRoot` and `$Bin` install-time expansion.
4. Add `payloadDirectory` with `App` as its compatibility default and route
   lifecycle payload operations through the normalized path without changing
   `$App`.
5. Add `$VersionRoot` and `$Payload` for paths beneath the active version and
   add distinct payload paths to hook contexts.
6. Preserve schema version 1 and package-local compatibility behavior.

Exit criteria: an ordinary package selected through manager v2 installs its
components only into the configured scope locations.

### Phase 2: standalone runtime and `gupkg` package

1. Add the locked embedded-Python assembly/build process.
2. Add the official `gupkg` bootstrap definition under `pkgs/`.
3. Add self status, install, and repair coordination.
4. Produce and test the bootstrap ZIP.
5. Implement compatible running-shim replacement behavior.

Exit criteria: a clean Windows machine with no Python can install and run
`gupkg` from either default scope.

### Phase 3: signed official registry

1. Define the index schema and release validation command.
2. Add deterministic snapshot publication and signing in CI.
3. Add verification, safe extraction, atomic cache publication, status, and
   offline behavior.
4. Publish the three stable-channel GitHub release assets.

Exit criteria: `gupkg registry sync` can obtain a verified snapshot without
executing its contents, and every failure preserves the preceding snapshot.

### Phase 4: registry search and install

1. Add catalog search and selector resolution.
2. Add root-level staging/locking and collision rules.
3. Dispatch a selected install seed through the normal install workflow.
4. Add user/system elevation and revalidation boundaries.
5. Add `self update` as a normal package update convenience.

Exit criteria: a user can sync, search, and install any valid official package
into the requested root, including `gupkg` itself.

### Phase 5: manager TUI, documentation, and migration

1. Add registry status, sync, search, Available filter, and scoped Install to
   the manager TUI.
2. Make the standalone bootstrap the primary README installation path.
3. Document config, cache, trust, offline use, coexistence, recovery, and
   migration.
4. Complete Windows clean-machine and self-update release smoke tests.

Exit criteria: CLI and TUI expose the same registry/install behavior and the
documented bootstrap works from the published GitHub release.

## Acceptance criteria

The design is implemented only when all of the following are true:

1. A user can install `gupkg` without a preinstalled Python or source checkout.
2. The standalone `gupkg` package remains usable from any extraction or
   user-selected installation directory.
3. User self-install creates `%USERPROFILE%\bin\gupkg.exe` by default without
   relocating the package.
4. System self-install creates `C:\bin\gupkg.exe` by default without placing a
   manager config in `C:\opt`.
5. The native shim invokes the selected version-local runtime without embedding a
   manager config path.
6. Configuration resolves from explicit path, current-directory marker,
   `%APPDATA%\gupkg\gupkg-config.toml`, then the active version's
   `gupkg-config.toml`, with built-in defaults last.
7. Manager TOML can change both package roots, both bin directories, and the
   registry cache within the documented safety constraints.
8. User and system shims may coexist without overwriting one another.
9. A clean bootstrap installs locally even if the later registry sync fails.
10. Registry sync verifies signed metadata, archive size/hash, extraction
   safety, indexed files, and package health before activation.
11. Failed synchronization leaves the previous verified snapshot usable.
12. Local package management never requires registry access.
13. Search and install can operate offline from a verified snapshot.
14. Registry sync/list/search never imports or executes package-local code.
15. Registry-backed install defaults to user scope and requires explicit
    system selection/elevation.
16. Registry installation stages one install seed and delegates to the
    existing package workflow rather than implementing a second installer.
17. Installed state remains filesystem-authoritative.
18. `gupkg` updates itself as a normal package without overwriting its running
    runtime or deleting the previous version.
19. The running native shim is either left intact or replaced safely after it
    exits.
20. Build and release automation produces the standalone package, bootstrap
    archive, signed registry assets, digests, and clean-machine test evidence.
21. The `gupkg` package version contains `gupkg\`, optional
    `gupkg-config.toml`, and standard `pkg.toml` without requiring an `App`
    directory.
22. `payloadDirectory = "gupkg"` lets lifecycle operations manage the sibling
    `gupkg` directory while `$App` continues to resolve only to `current\App`.
23. `$VersionRoot\gupkg` gives shims and environment entries a direct path to
    embedded support without depending on `$App\..` or an existing `App`.
24. `$Payload`, `PkgVars.Payload`, and `paths.stagePayload` expose the custom
    lifecycle payload without rebinding legacy App values.
25. Source and registry trees omit `gupkg\python`, while standalone release
    artifacts contain a verified runtime and local bootstrap uses atomic
    repair.
26. Missing runtimes, moved packages, duplicate shims, concurrent self-updates,
    config shadowing, reparse points, and partial downloads follow the explicit
    corner-case rules above.
27. External `gupkg.cmd` and `gupkg-tui.cmd` perform only local-executable
    selection, PATH-only fallback, argument forwarding, and exit propagation.
28. A selected local executable failure never falls through to another
    `gupkg` installation.
29. Unavoidable no-Python bootstrap logic lives in internal command files, and
    every step that can run after an interpreter starts is owned by Python.
30. The TUI wrapper selects local `gupkg-tui.exe` or invokes system
    `gupkg.exe tui` without duplicating runtime/bootstrap policy.

## Final expected experience

A first-time user downloads one ZIP from the project's GitHub release,
extracts it, and runs:

```text
gupkg-bootstrap.exe --scope user
```

The standalone package remains in the directory where the user extracted or
moved it. Its version directory contains `pkg.toml`, optional
`gupkg-config.toml`, and the `gupkg\` application payload with a locally
vendored `gupkg\python` runtime. Bootstrap creates
`%USERPROFILE%\bin\gupkg.exe`, adds that directory to the user `PATH`, and
downloads the verified official registry. A persistent user override may be
placed at `%APPDATA%\gupkg\gupkg-config.toml`. After opening a new terminal,
the user can run:

```text
gupkg search vscode
gupkg install vscode
gupkg list
gupkg upgrade check
gupkg self update
```

An administrator can choose `--scope system` to create `C:\bin\gupkg.exe` and
the machine `PATH` entry while leaving the package itself wherever it was
placed. Managed applications still default to `C:\opt` for system scope and
`%USERPROFILE%\opt` for user scope. In both cases `gupkg` is itself an ordinary
managed package, and the registry is only a verified source of install
definitions rather than a second source of truth for the machine.
