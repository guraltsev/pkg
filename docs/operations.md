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
| Install gupkg itself and set up manager mode | `gupkg-bootstrap.cmd [-Scope user|system]` |
| Create the manager configuration | `gupkg manager init` |
| Install or repair one package | `gupkg install [PATH]`, `gupkg config-fix [PATH]` |
| Inspect or update one package | `gupkg config-check [PATH]`, `gupkg update [PATH]` |
| Inspect all managed packages | `gupkg manager list`, `gupkg manager doctor` |
| Check or apply managed updates | `gupkg manager update [--check-only|--download-only]` |
| Find packages in the registry | `gupkg manager registry sync`, `gupkg manager search` |
| Install a registry selector | `gupkg --scope user|system manager install SELECTOR` |
| Open an interactive interface | `gupkg tui [PATH]`, `gupkg manager tui` |

Every invocation names its command explicitly. Package paths are optional only
when the current directory resolves as a package. Manager selection never
occurs implicitly from the current directory.

## Standalone installation

`gupkg-bootstrap.ps1` (with its `gupkg-bootstrap.cmd` launcher, which only
bypasses the script execution policy for one run) is the supported entry point.
It runs on stock Windows PowerShell 5.1:

```bat
gupkg-bootstrap.cmd                      :: user scope, no elevation
gupkg-bootstrap.cmd -Scope system        :: all users, elevated shell required
gupkg-bootstrap.cmd -Source \\server\share\gupkg-0.12.0.zip -Sha256 <digest>
```

What it does, in order: choose the root (`%USERPROFILE%\opt` or `C:\opt`, or
`-Root`); fetch the archive from `-Source` (a URL, ZIP, or folder; default: the
`stable` tag of the official repository) and verify `-Sha256` when given;
place it at `<root>\gupkg\v<version>`, reusing a matching folder unless
`-Force`; run `gupkg install` on it, which creates the `current` junction, the
`gupkg` and `gupkg-tui` commands, and the PATH entry; and run
`gupkg manager init` unless `-SkipManagerInit` (an existing configuration is
left untouched). Every step is repeatable. It needs no Python in advance: the
first run of gupkg downloads a SHA-256-verified embedded Python when none is
installed. Keep the version directory immutable; repairs and upgrades should
replace or activate a complete version rather than editing the runtime in place.

Release archives contain a version directory with the application payload, the
embedded Python runtime, package metadata, and the native shim files. They ship
no manager configuration: roots and registry settings belong to the
administrator and are created by `manager init`.

The repository-side assembly tool is:

```bat
python tools\build_standalone.py ^
  --runtime C:\release-inputs\cpython-3.12.10-embed-amd64.zip ^
  --runtime-sha256 <64-hex-digest> ^
  --source src\gupkg ^
  --manifest src\gupkg\pkg.toml ^
  --output dist\gupkg-0.12.0.zip
```

The runtime archive and digest are explicit inputs. The script verifies the
digest, excludes a source-checkout `python` directory, copies the audited
native shims/configuration, injects the runtime under the payload, and writes a
versioned ZIP. It does not silently download or trust an arbitrary Python
runtime. The outer release bootstrap executable is a release packaging concern;
the ZIP is the repository build output.

For a source checkout, the existing `src\gupkg.cmd` launcher remains available
for development. It selects a packaged `gupkg\gupkg.exe` when present and
otherwise delegates to the adjacent `gupkg\gupkg.cmd` bootstrap. The bootstrap
may use `GUPKG_PYTHON`, `gupkg.python`, a local `python\python.exe`, Python on
`PATH`, or the Windows `py -3` launcher.

## Manager configuration

Manager mode is enabled by a v2 `gupkg-config.toml`. The discovery order is:

1. an explicit `--config PATH`;
2. the file beside the imported `gupkg.cli` module;
3. `%GUPKG_HOME%\gupkg-config.toml`;
4. `%APPDATA%\gupkg\gupkg-config.toml`.

The first existing candidate wins. An invalid higher-priority candidate is an
error and never falls through. The current directory and its parents are
never searched; a missing candidate is reported with every searched path.

The `gupkg-tui` launcher is a convenience wrapper for the package `tui`
command; it does not select manager mode implicitly. Use `gupkg manager tui`
when manager mode is intended. Without a package path, package `tui` requires
the current directory to resolve as a package.

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

# Optional. These are the defaults; change them to move the cache or use a mirror.
[registry]
cache = '%LOCALAPPDATA%\gupkg\registry'
source = "https://github.com/guraltsev/pkg/archive/refs/tags/stable.zip"
```

`packages.system` and `packages.user` are required, distinct, non-nesting
collection roots. The two `bin` directories must also be distinct. The whole
`[registry]` table is optional: `cache` defaults to
`%LOCALAPPDATA%\gupkg\registry` and must be outside both package roots, and
`source` defaults to the official `stable` archive. `source` may be any
`https://` or `file:` URL of a ZIP archive that contains a `pkgs` folder, so a
mirror or a network share works. The obsolete `channel = "stable"` line is
accepted and ignored. Existing configured paths must be
directories; loading the configuration does not create roots. Relative paths
are resolved relative to the configuration file, `%NAME%` references use the
case-insensitive process environment, and a leading `~` means the current
user's home directory. Unknown variables, shell substitutions, and unsafe path
relationships are rejected.

Only schema version 2 is accepted. Older files must be converted as a separate
administrative operation before they can select manager mode.

## First-time setup

```bat
gupkg manager init            :: write gupkg-config.toml with defaults and create its folders
gupkg manager registry sync   :: download the package catalogue
gupkg manager doctor          :: confirm everything is healthy
```

`manager init` refuses to overwrite an existing configuration unless you pass
`--force`, and reports (without failing) any folder it could not create, such as
the system root without Administrator rights. Edit the file afterwards to move
any location.

## Registry workflow

The official stable registry is the `pkgs` folder of the `stable` tag of
`github.com/guraltsev/pkg`, fetched as a plain ZIP archive over HTTPS (or from
the `[registry] source` you configure) using Python's standard library; neither
Git nor any other tool is required. Registry data is treated as untrusted
metadata: sync extracts only the `pkgs` folder with path-safety checks,
validates manifests and selectors, and does not import or execute package code.
Each archive is identified by its commit ID (or content hash), and a failed sync
leaves the previous validated tree in place.

```bat
gupkg manager registry sync
gupkg manager registry status
gupkg manager search ripgrep
gupkg manager search --offline
```

Use `--offline` with `search` when network access is forbidden. Offline search
uses only the last validated cache. `registry status` reports cache and active
tree state without downloading.

Install a registry package by its selector:

```bat
gupkg --scope user manager install ripgrep
gupkg --scope user manager install vscode --offline
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
gupkg manager self status
gupkg --scope user manager self repair
gupkg --scope system manager self repair
gupkg --scope user manager self update
```

`manager self status` is read-only. `manager self repair` restores missing or inconsistent
standalone launch metadata and shims from the installed version. `self update`
uses the same self-package path as repair; a newer release must be present in
the configured release/update source before it can be activated. System scope
requires elevation. If the embedded runtime itself is missing or corrupted,
reinstall the complete standalone release rather than copying individual
Python files.

## Routine recovery

When a manager command fails, preserve the evidence before changing anything:

1. Run `gupkg manager registry status` and `gupkg manager doctor`.
2. Run `gupkg --format toml manager list` to capture scoped inventory and health fields.
3. For a registry problem, retry `gupkg manager registry sync`; if it fails, continue
   using the previous validated cache or add `--offline`.
4. For a package problem, fix the package root or manifest, then run
   `gupkg config-check <path>` and `gupkg install <path>`.
5. For a standalone problem, run `gupkg manager self status`, then the appropriate
   scoped `manager self repair`.

Manager update planning is non-mutating. Use `gupkg --scope auto manager update
--check-only` before a confirmed full update; a failed target does not invalidate successful targets,
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
