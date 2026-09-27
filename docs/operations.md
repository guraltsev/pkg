# Operations guide

This is the operational guide for the standalone Windows distribution and the
central manager. The top-level [README](../README.md) explains the package
model; this page is the runbook for installing, configuring, repairing, and
operating `gupkg`.

## Choose an operating mode

Use package-local mode when you already have a package directory and want to
install or repair that one package. Use manager mode when one centrally
installed `gupkg` should discover packages in separate user and system roots.

| Need | Entry point |
| --- | --- |
| Install or repair one package | `gupkg install <version-dir-or-root>` |
| Inspect all managed packages | `gupkg list`, `gupkg doctor` |
| Check or apply managed updates | `gupkg upgrade check`, `gupkg upgrade all` |
| Find packages in the GitHub registry | `gupkg registry sync`, `gupkg search` |
| Install a registry selector | `gupkg install <selector>` |
| Check or repair the standalone runtime | `gupkg self status`, `gupkg self repair` |

Local paths continue to mean local packages. In manager mode, a bare token such
as `ripgrep` is a registry selector; an existing path or path-looking argument
continues to use the local package workflow.

## Standalone installation

Release artifacts contain a version directory with the application payload,
the embedded Python runtime, package metadata, and the native bootstrap/shim
files. The release wrapper is the supported entry point. It selects a user or
system install location and then runs the packaged manager:

```bat
gupkg-bootstrap.exe --scope user
gupkg-bootstrap.exe --scope system
```

User scope does not require elevation. System scope requires an elevated
terminal or an accepted UAC prompt. After installation, use the installed
`gupkg` command and keep the version directory immutable; repairs and upgrades
should replace or activate a complete version rather than editing the embedded
runtime in place.

The repository-side assembly tool is:

```bat
python tools\build_standalone.py ^
  --runtime C:\release-inputs\cpython-3.12.10-embed-amd64.zip ^
  --runtime-sha256 <64-hex-digest> ^
  --source . ^
  --manifest pkgs\gupkg\vbootstrap.l1\pkg.toml ^
  --version 0.1.0 ^
  --output dist\gupkg-0.1.0.zip
```

The runtime archive and digest are explicit inputs. The script verifies the
digest, excludes a source-checkout `python` directory, copies the audited
native shims/configuration, injects the runtime under the payload, and writes a
versioned ZIP. It does not silently download or trust an arbitrary Python
runtime. The outer release bootstrap executable is a release packaging concern;
the ZIP is the repository build output.

For a source checkout, the existing `src\gupkg\gupkg.cmd` launcher remains
available for development. It may use `GUPKG_PYTHON`, `gupkg.python`, a local
`python\python.exe`, or Python on `PATH`.

## Manager configuration

Manager mode is enabled by a v2 `gupkg-config.toml`. The discovery order is:

1. an explicit `--config PATH`;
2. `gupkg-config.toml` in the current directory;
3. `%APPDATA%\gupkg\gupkg-config.toml`;
4. the version-local manager configuration beside the installed executable.

The first existing candidate wins. If no candidate exists, package-local mode
is used and manager-only commands fail with a configuration error rather than
creating a default manager.

Use this shape for a new manager configuration:

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

`packages.system` and `packages.user` are required, distinct, non-nesting
collection roots. The two `bin` directories must also be distinct. The registry
cache must be outside both package roots. Existing configured paths must be
directories; loading the configuration does not create roots. Relative paths
are resolved relative to the configuration file, `%NAME%` references use the
case-insensitive process environment, and a leading `~` means the current
user's home directory. Unknown variables, shell substitutions, and unsafe path
relationships are rejected.

Schema v1 remains readable for migration, but it has no `[bin]` or `[registry]`
section. Validate and rewrite it with:

```bat
gupkg --config C:\path\gupkg-config.toml migrate-config
```

Migration rewrites the selected file atomically. Copy the original first when
you need a rollback artifact, then review the generated v2 file and create
missing directories deliberately.

## Registry workflow

The official stable registry is the Git repository
`https://github.com/guraltsev/pkg.git`. Registry data is treated as untrusted
metadata: sync uses a sparse checkout, validates manifests and selectors, and
does not import or execute package code. A failed sync leaves the previous
validated active tree in place.

```bat
gupkg registry sync
gupkg registry status
gupkg search ripgrep
gupkg search --installed
gupkg search --available
```

Use `--offline` with `search` when network access is forbidden. Offline search
uses only the last validated cache. `registry status` reports cache and active
tree state without downloading.

Install a registry package by its selector:

```bat
gupkg install ripgrep
gupkg install editors/vscode --scope user
gupkg install ripgrep --offline
```

The selector resolves to a validated manifest, then the normal package install
workflow applies its origin and integration declarations. `--offline` allows
use of the registry cache but does not make a missing application payload
available: an origin still needs its own local files or cached/offline-capable
source. Local package paths never contact the registry.

## Self-health and recovery

The self commands inspect the embedded runtime, bootstrap metadata, and the
scope-specific shim/configuration pair:

```bat
gupkg self status
gupkg self repair --scope user
gupkg self repair --scope system
gupkg self update --scope user
```

`self status` is read-only. `self repair` restores missing or inconsistent
standalone launch metadata and shims from the installed version. `self update`
uses the same self-package path as repair; a newer release must be present in
the configured release/update source before it can be activated. System scope
requires elevation. If the embedded runtime itself is missing or corrupted,
reinstall the complete standalone release rather than copying individual
Python files.

## Routine recovery

When a manager command fails, preserve the evidence before changing anything:

1. Run `gupkg registry status` and `gupkg doctor`.
2. Run `gupkg list --toml` to capture scoped inventory and health fields.
3. For a registry problem, retry `gupkg registry sync`; if it fails, continue
   using the previous validated cache or add `--offline`.
4. For a package problem, fix the package root or manifest, then run
   `gupkg config check <path>` and `gupkg install <path>`.
5. For a standalone problem, run `gupkg self status`, then the appropriate
   scoped `self repair`.

Manager upgrade planning is non-mutating. Use `gupkg upgrade check` before
`gupkg upgrade all`; a failed target does not invalidate successful targets,
and a later safe rerun revalidates already-current packages. Declining mixed
scope elevation must leave user packages unchanged.

## Release and validation checks

Before publishing registry or standalone changes, run:

```bat
python tools\validate_registry.py pkgs
python -m pytest -q
```

The registry validator checks every manifest and selector without importing
package code. The test suite covers manager schema/discovery, registry parsing
and cache behavior, standalone assembly, payload variables, and the existing
package/update workflows. The Windows-specific cases in
`tests\manual_smoke.md` still require a real Windows environment.
