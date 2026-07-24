# Blueprint: automatic package updates and safe self-update

Date: 2026-07-24
Priority: High
Change type: Feature design

## Goal

Allow `pkg` to discover, download, prepare, and activate new application
versions without requiring a package author to build each version directory by
hand.

The feature must support four related workflows:

1. check whether an application has an update
2. automatically apply an available update when package policy permits it
3. download and unpack a new `App/` payload into a new version directory
4. update `pkg` itself without overwriting the files of the running process

The design treats two application layouts as standard built-in workflows:

1. `App/` is a Git checkout. `pkg` compares the checkout with a configured
   remote ref and creates a fresh checkout for the candidate commit.
2. `App/` contains files extracted from a downloaded zip archive. `pkg` checks
   for the latest release through a package-local Python module, downloads and
   verifies the candidate zip, and extracts its contents into staged `App/`.

For the Git workflow, `pkg` assigns new versions an upstream version in this
UTC timestamp-plus-source format:

```text
YYYYMMDD-HHMMSS-git
```

The complete directory name remains compatible with the existing package
layout:

```text
vYYYYMMDD-HHMMSS-git.l1
```

Packages define Python modules for downloadable-version discovery and may
define another Python module for nonstandard unpacking.

## Design summary

Add an optional `[update]` table to `pkg.toml`.

Update handling has three explicit phases:

```text
check -> prepare -> activate
```

- **Check** produces a normalized update candidate and does not modify the
  installed package.
- **Prepare** creates a complete candidate version in a package-root staging
  directory. Downloads and unpack modules write only into staging.
- **Activate** moves the prepared version into its final version directory and
  delegates to the existing install workflow to update `current` and repair
  components.

Git checkout discovery is built in. Downloadable release discovery is always
implemented by a package-local Python module loaded into the running `pkg`
process. The module returns an in-memory candidate mapping; it is not executed
as a subprocess and there is no remote manifest protocol.

The candidate can feed the built-in zip downloader and extractor. A second
package-local Python module is needed only for archive formats or layouts that
built-in zip extraction cannot represent.

Package-local Python modules live under a reserved version-local directory:

```text
<version>/pkg.local/
```

`pkg` loads modules from exact file paths with `importlib`; it never executes
package update hooks as shell commands or standalone scripts.

The system recognizes these conventional filenames:

| File | Recognized callable | Purpose |
|---|---|---|
| `pkg.local/check_update.py` | `check_update(context)` | Find the latest downloadable version |
| `pkg.local/unpack_app.py` | `unpack_app(context)` | Populate staged `App/` from a verified download |
| `pkg.local/populate_app.py` | `populate_app(context)` | Recreate `App/` for the exact installed version |

When the corresponding configuration selects module behavior and omits a
`module` path, `pkg` uses the conventional filename.

These names are conventions, not a whitelist. A package may:

- select another `.py` file below `pkg.local/` with an explicit `module` field
- organize modules in subdirectories below `pkg.local/`
- include any number of helper modules with package-chosen names
- use relative imports between its local modules

For example:

```text
pkg.local/
  check_update.py          # recognized automatically
  unpack_app.py            # recognized automatically
  populate_app.py          # recognized automatically
  release_site.py          # package-chosen helper
  formats/
    vendor_archive.py      # package-chosen helper or explicit hook
```

`pkg` creates a unique internal package namespace for each resolved
`pkg.local/` directory so relative imports work without adding the directory
globally to `sys.path`. Files are still loaded as modules in the running
interpreter, never executed as shell or standalone Python scripts.

Modules are loaded fresh for each public action. After the action finishes,
`pkg` removes only the unique package-local namespace it created from
`sys.modules`; it does not remove unrelated modules or modify global import
paths. Module-level mutable state must not be used as persistent package state.
Persistent values belong in TOML files owned by `pkg`.

Manager-owned state, locks, receipts, logs, caches, and temporary update
directories live separately:

```text
<package-root>/.pkg/
```

Automatic checking is policy, not a separate update implementation. Ordinary
root/current installs perform a due check using a fixed internal check
interval. A package has one automatic-policy setting: whether an available
candidate may be applied without manual intervention. `pkg` does not create or
manage background jobs or schedules.

Self-update uses the same check, prepare, validate, and activation engine. A
stable launcher outside the version directories starts `pkg` through its
`current` junction, so the running version is never overwritten.

## Terms and directory ownership

### Package root

The directory containing all versions and the `current` junction:

```text
Ripgrep/
```

### Version directory

One immutable installed version containing only package-authored configuration,
local modules, resources, and the finished application payload:

```text
Ripgrep/v14.1.0.l1/
```

A finalized version directory must never contain:

- partial downloads or archives
- staging or extraction directories
- locks, logs, caches, or update state
- update receipts generated by `pkg`
- `__pycache__/`, `.pyc`, `.tmp`, `.part`, backup, or rollback files
- a file created merely to coordinate an in-progress operation

This is a hard invariant checked immediately before the staged tree is renamed
to its final version name.

### Version skeleton

The package-authored files copied from the current version when preparing a new
one. It includes `pkg.toml`, `Icons/`, `Shortcuts/`, `pkg.local/`, and any other
package-specific support files. It excludes:

- `App/`
- temporary or staging paths owned by `pkg`
- Python bytecode caches

### Package update state

All manager-owned data uses this exact package-root layout:

```text
<package-root>/.pkg/
  work/
    <operation-id>/
      download/
      extract/
      version/
      pycache/
      receipt.toml
  locks/
    update.toml
  state/
    update.toml
  receipts/
    v2.4.1.l1.toml
  logs/
  backups/
```

Directory roles:

- `work/`: disposable same-volume staging, partial downloads, extraction trees,
  staged version trees, and bytecode generated while loading `pkg.local`
  modules
- `locks/`: operation coordination files created with exclusive semantics
- `state/`: due-check timing, candidate assignment, and last-result state
- `receipts/`: durable candidate/source records keyed by finalized version
- `logs/`: optional operation diagnostics when file logging is enabled
- `backups/`: durable config backups when an action explicitly preserves the
  previous file

No manager-owned file may be created directly in the package root, a concrete
version directory, `current`, or `App/`.

Package repositories can ignore all generated manager data with one anchored
rule:

```gitignore
/.pkg/
```

If a repository intentionally preserves durable state or receipts, it may use
more precise rules instead:

```gitignore
/.pkg/work/
/.pkg/locks/
/.pkg/logs/
/.pkg/state/
/.pkg/receipts/
/.pkg/backups/
```

The entire `.pkg/` tree must never be copied into a version directory and must
never be reachable through `$App`, `$Icons`, or `$Shortcuts`.

This layout applies to existing mutation paths as they are revised, not only to
new Update actions:

- origin download and extraction work moves from the version directory into
  `.pkg/work/<operation-id>/`
- `App/` replacement keeps its prepared and rollback directories under the
  operation work directory
- config atomic-write temporaries live under the operation work directory;
  requested backups live under `.pkg/backups/`
- temporary `current` junctions and rollback junctions live under the
  operation work directory
- imported package modules send bytecode caches to the operation work
  directory

Atomic replacement may move a completed file or directory from `.pkg/work/`
into its final location because both are on the same volume. It must not create
a temporary sibling beside `pkg.toml`, `App/`, `current`, or a version
directory.

### Update candidate

A normalized description of one available upstream state. It includes:

- a stable candidate ID
- the proposed upstream version
- the payload acquisition information
- optional release metadata for display

Candidate identity and display version are separate. For example, a Git
candidate ID is the commit hash while its generated version is a timestamp.
This prevents repeated checks of the same commit from creating new versions.

## Standard workflows

Update discovery and payload preparation are separate choices. The supported
combinations are:

| `App/` layout                     | Update check              | Payload preparation              |      Version source       |
| --------------------------------- | ------------------------- | -------------------------------- | :-----------------------: |
| Git checkout                      | built-in Git check        | exact Git checkout               | UTC `YYYYMMDD-HHMMSS-git` |
| Extracted zip                     | local Python check module | verified built-in zip extraction |  module result `version`  |
| Other archive or generated layout | local Python check module | local Python unpack module       |  module result `version`  |

The second row is as fundamental as the Git workflow. A normal release package
does not need a custom unpack module merely because `App/` is extracted rather
than a checkout.

For an extracted-zip package, the installed layout is:

```text
Tool/
  current/
  v2.4.1.l1/
    App/
      tool.exe
      supporting.dll
    pkg.toml
```

The downloaded zip itself is temporary. It is verified and extracted under
`<package-root>/.pkg/work/<operation-id>/`, then removed after the prepared version is
committed. `App/` contains the extracted application files, not the archive.

## Scope

### In scope

- one-package update checks
- Git checkout discovery and acquisition
- package-local Python module discovery for downloadable releases
- HTTP(S) archive download
- built-in safe zip extraction
- custom Python check modules
- custom Python unpack modules
- staged, atomic version creation
- automatic check/apply policy
- `pkg` self-update through a stable launcher
- update state, locking, receipts, retry, and rollback behavior

### Non-goals

- a central package repository or dependency solver
- update ordering across dependent packages
- delta or binary patch formats
- arbitrary remote installer execution
- silently installing unsigned `pkg` self-updates
- deleting old versions as part of update activation
- isolating package-authored Python modules from the running process
- creating or managing scheduled tasks, cron jobs, or background services
- changing the existing `v<upstream>.l<local>` directory convention

## Configuration

The `[update]` table is independent from `[origin]`.

- `[origin]` answers: “How can this exact version repopulate its `App/`?”
- `[update]` answers: “How do I discover and construct a newer version?”

Keeping those questions separate prevents install/reinstall behavior from
performing an implicit version change.

### Shared update policy

```toml
[update]
allow_automatic_update = false
```

Fields:

- `allow_automatic_update`: Optional boolean, default `false`. When true, a due
  check performed by Install or `AutoUpdate` may prepare and activate an
  available candidate without confirmation. When false, automatic actions may
  check and report but cannot download or activate the candidate.

This is the only configurable automatic-update policy. Check timing uses one
fixed internal interval, initially 24 hours. It is not configurable in
`pkg.toml`. Explicit `CheckUpdate` and `Update` actions ignore the due-check
timestamp.

An ordinary Install performs a due check only when the caller targets the
package root or `current`. Installing an explicit old version remains an
explicit repair operation and must not unexpectedly switch versions.

### Git checkout mode

```toml
[update]
allow_automatic_update = true

[update.check]
mode = "git"
appPath = "App"
remote = "origin"
ref = "refs/heads/main"

[update.payload]
mode = "git"
```

Fields:

- `mode`: Required and equal to `git`.
- `appPath`: Optional relative path to the checkout, default `App`. It must
  resolve inside the concrete version directory.
- `remote`: Optional Git remote name, default `origin`.
- `ref`: Required full remote ref, such as `refs/heads/main` or
  `refs/tags/v2.0.0`.

The built-in check:

1. resolves `appPath` inside the current version
2. requires it to be a Git work tree
3. reads the local `HEAD`
4. obtains the configured remote URL
5. runs `git ls-remote --exit-code <url> <ref>`
6. compares the returned object ID with local `HEAD`

The check does not fetch into or otherwise change the installed checkout.

When the object IDs differ, the candidate is:

```toml
status = "available"
candidateId = "git:<40-or-64-character-object-id>"
version = "20260724-184530-git"

[payload]
kind = "git"
url = "https://example.invalid/project.git"
ref = "refs/heads/main"
commit = "<object-id>"
```

The timestamp is generated from the current UTC clock at candidate creation.
`pkg` stores the candidate ID in update state, so rechecking the same commit
reuses the assigned version.

Git payload preparation performs a fresh checkout in staging and checks out the
exact candidate commit. It does not pull, reset, or clean the existing `App/`.
The staged `App/` remains a Git checkout, including `.git`, because that is the
input to the next update check.

If the final `v<timestamp>-git.l1` path already exists:

- the same candidate receipt makes the update idempotently current
- a different candidate using the same timestamp receives `.l2`, then `.l3`,
  and so on

The upstream version remains the required timestamp with its `-git` suffix;
the existing local revision disambiguates collisions.

### Downloadable zip mode

```toml
[update]
allow_automatic_update = false

[update.check]
mode = "module"
# module = "pkg.local/check_update.py"  # recognized default
channel = "stable"

[update.payload]
mode = "zip"
extractSubdir = "tool-portable"
ignore_checksum = false
```

Fields:

- `[update.check].module`: Optional path to the local Python module that
  determines the newest downloadable release. Default
  `pkg.local/check_update.py`.
- `[update.check].channel`: Optional package-defined selector passed to the
  module, default `stable`. It selects an update source; it is not automatic
  scheduling policy.
- `[update.payload].mode`: Required and equal to `zip` for built-in
  extraction.
- `[update.payload].extractSubdir`: Optional default archive subdirectory.
  A module result can override it for one candidate.
- `[update.payload].ignore_checksum`: Optional version-owned boolean, default
  `false`. When false, the candidate must contain `sha256` and verification is
  mandatory. When true, a candidate without a checksum is allowed and checksum
  verification is skipped.

The configured module defines:

```python
PKG_MODULE_API = 1


def check_update(context):
    """Return the newest downloadable release or ``None`` when current."""

    # This example can inspect a release page, API, directory listing, or
    # package-specific source. Network and parsing policy belong to the module.
    latest = find_latest_release(context["channel"])
    if latest["version"] == context["current"]["version"]:
        return None

    return {
        "candidateId": f"release:{latest['version']}",
        "version": latest["version"],
        "url": latest["url"],
        "sha256": latest.get("sha256"),
        "fileName": latest["file_name"],
        "extractSubdir": latest.get("extract_subdir"),
        "publishedAt": latest.get("published_at"),
        "notesUrl": latest.get("notes_url"),
    }
```

The returned mapping always requires:

- `version`
- `candidateId`
- `url`

It may also contain:

- `sha256`
- `fileName`
- `extractSubdir`
- `publishedAt`
- `notesUrl`

The returned version must be safe in a version directory and must compare newer
than the current upstream version using `compare_package_versions`. An equal
version with a different candidate ID is a republished artifact and fails
closed. It is not silently assigned a new local revision. `pkg` validates the
mapping before performing any download.

`sha256` is required unless the current version's
`[update.payload].ignore_checksum` is explicitly `true`. The check module
cannot enable this exception through its return value.

The existing `--no-checksum` CLI option is also an explicit per-invocation
bypass. Therefore a download may proceed when either:

- the current version declares
  `[update.payload].ignore_checksum = true`, or
- the user invoked the action with `--no-checksum`

Both forms apply to explicit Update, AutoUpdate, and SelfUpdate. They must emit
a prominent warning and record which bypass was used in the root-owned receipt.

### Python check module contract

```toml
[update.check]
mode = "module"
# module = "pkg.local/check_update.py"  # recognized default

[update.payload]
mode = "zip"
```

Rules:

- The path must be relative.
- It must resolve under `<version>/pkg.local/`.
- It must end in `.py`.
- `pkg` loads it from the exact resolved path with
  `importlib.util.spec_from_file_location`.
- It is assigned a unique internal module name derived from the resolved path
  and file content hash.
- `pkg` exposes `pkg.local/` only through a unique internal package namespace;
  it does not add the directory globally to `sys.path`.
- It must declare `PKG_MODULE_API = 1`.
- It must expose `check_update(context)`.
- It returns `None` for no update or a candidate mapping for an available
  update.
- Import errors, exceptions, unsupported API versions, or invalid return values
  fail the check.

The context is an ordinary in-memory Python mapping:

```python
{
    "apiVersion": 1,
    "channel": "stable",
    "current": {
        "name": "Tool",
        "version": "2.3.0",
        "localVersion": 1,
        "versionString": "v2.3.0.l1",
        "candidateId": "release:2.3.0",
    },
    "paths": {
        "packageRoot": Path(r"C:\Packages\Tool"),
        "versionRoot": Path(r"C:\Packages\Tool\v2.3.0.l1"),
        "app": Path(r"C:\Packages\Tool\v2.3.0.l1\App"),
    },
    "state": {
        "lastSuccessfulCheck": datetime(...),
        "lastCandidateId": "release:2.3.0",
    },
}
```

`pkg` passes defensive copies of config and state. Paths are `pathlib.Path`
objects and timestamps are timezone-aware `datetime` objects, avoiding a
serialization round trip. `candidateId`, `version`, and all payload fields are
validated by `pkg`, not trusted as filesystem paths.

The module is expected not to mutate package files. This is a contract, not a
sandbox guarantee. It is imported into the `pkg` process and therefore has the
same operating-system permissions and access to interpreter state as `pkg`.
Only trusted package modules may be configured.

### Custom Python unpack module

```toml
[update.payload]
mode = "module"
# module = "pkg.local/unpack_app.py"  # recognized default
```

The downloader remains owned by `pkg`. The check result must provide an
HTTP(S) `payload.url` and, unless version policy or `--no-checksum` bypasses
verification, a SHA-256 checksum. `pkg` downloads and conditionally verifies
the artifact before calling the unpack module.

The module must declare `PKG_MODULE_API = 1` and expose:

```python
def unpack_app(context):
    """Populate ``context["paths"]["stageApp"]`` from the verified artifact."""
```

Its context contains the normalized candidate plus `Path` objects named
`artifact`, `stageRoot`, and `stageApp`. Returning `None` means success; an
exception means failure. The module must populate `stageApp` and must not write
to the installed
version's `App/`. After success, `pkg` verifies that:

- `stageApp` exists
- `stageApp` is a real directory, not a junction or symlink
- it resolves inside `stageRoot`
- it is non-empty
- the module did not replace `stageRoot` or the artifact path with reparse
  points

These checks catch accidents; they do not make untrusted Python safe.

### Invalid combinations

Configuration validation rejects:

- unknown keys or modes
- both Git check fields and a Python check module
- `payload.mode = "git"` with a non-Git candidate
- `payload.mode = "zip"` without an archive URL
- `payload.mode = "module"` when neither an explicit module nor the recognized
  default `pkg.local/unpack_app.py` exists
- module paths outside `pkg.local/`
- absolute or parent-traversing `appPath` and `extractSubdir`
- non-boolean `allow_automatic_update`

Do not introduce provider registries or plugin base classes. Normalize the
two check modes and three payload modes into small dictionaries, then use
direct branches in the coordinating workflow.

## Relationship to `[origin]`

Every newly prepared version must retain a valid reinstall story.

### Git updates

The new version keeps the same generic Git update configuration. Its exact
checked-out commit and remote URL are recorded outside the version directory:

```text
<package-root>/.pkg/receipts/v20260724-184530-git.l1.toml
```

Git packages should use a generic package-local `[origin].module` when they
want `--refresh-app` to recreate the checkout:

```toml
[origin]
mode = "module"
# module = "pkg.local/populate_app.py"  # recognized default
```

The loaded module receives the root-owned TOML receipt path in its context and
checks out the exact commit.
`[origin].module` can name another file below `pkg.local/`.
It declares `PKG_MODULE_API = 1` and exposes `populate_app(context)`.
Canonical package-authored extension points use imported Python modules; they
do not execute `.ps1`, `.cmd`, `.bat`, `.exe`, or standalone `.py` scripts.
Existing `[origin].script` packages require a documented migration to
`[origin].module` before this design is complete.

### Downloaded archive updates

When a check module supplies an exact archive URL and checksum, `pkg`
updates the new version's `[origin]` source to those exact values:

```toml
[origin]
url = "https://example.invalid/tool-2.4.1.zip"
checksum = "sha256:<64 hex characters>"
extractSubdir = "tool-2.4.1"
ignore_checksum = false
```

If the update was permitted without a checksum, the generated exact-version
origin instead contains:

```toml
[origin]
url = "https://example.invalid/tool-2.4.1.zip"
extractSubdir = "tool-2.4.1"
ignore_checksum = true
```

`[origin].ignore_checksum` is version-specific. Origin population requires a
valid checksum unless that exact version explicitly sets it to `true` or the
current invocation uses `--no-checksum`.

When `--no-checksum` bypasses verification:

- retain a supplied checksum in the generated `[origin]`, so a later refresh
  verifies it normally
- if the candidate supplied no checksum, write `ignore_checksum = true` into
  the new version's `[origin]`, because otherwise that exact version could not
  be repopulated

The previous inline source is retained as a `[[origin.versions]]` entry if it
is not already present. The change is limited to the `[origin]` region and the
owned top-level `version` and `localVersion` fields. Text outside those owned
regions remains byte-for-byte unchanged.

If this preservation cannot be implemented reliably, the first archive-update
implementation must fail with a clear message instead of copying a stale
origin into the new version.

### Custom unpack updates

The exact artifact metadata is written to the version's root-owned receipt
under `.pkg/receipts/`. A package that needs `--refresh-app` must also configure
an `[origin].module` capable of repeating the custom extraction. Update unpack
modules are not implicitly used by Install because their contract writes into
work staging rather than directly into a concrete version.

## Version receipt

Every successfully committed version has a separate receipt:

```text
<package-root>/.pkg/receipts/<version-directory-name>.toml
```

For example:

```toml
schemaVersion = 1
candidateId = "git:abc123..."
version = "20260724-184530-git"
localVersion = 1
checkedAt = 2026-07-24T18:45:30Z
preparedAt = 2026-07-24T18:45:42Z
checkMode = "git"
payloadMode = "git"
checksumPolicy = "not-applicable"

[source]
url = "https://example.invalid/project.git"
ref = "refs/heads/main"
commit = "abc123..."
```

The receipt:

- makes apply idempotent
- links a timestamp version to an exact Git commit
- supports health diagnostics and rollback reporting
- records `checksumPolicy` as `verified`, `version-ignore`, `cli-bypass`, or
  `not-applicable`
- must not contain credentials, authorization headers, or signed query strings

URLs are sanitized before persistence and logging. If a source URL contains
credentials, the update fails and asks the user to configure credentials
through Git or the operating system instead.

## Manager-owned state

`<package-root>/.pkg/state/update.toml` stores only coordination state:

```toml
schemaVersion = 1
lastAttemptedCheck = 2026-07-24T18:45:29Z
lastSuccessfulCheck = 2026-07-24T18:45:30Z
lastStatus = "available"
lastCandidateId = "git:abc123..."

[[assignedVersion]]
candidateId = "git:abc123..."
version = "20260724-184530-git"
```

Optional values such as `lastError` are omitted when absent because TOML has no
null value.

Rules:

- State is written atomically using the existing atomic write helper.
- A failed check records `lastAttemptedCheck` and `lastError` but does not
  advance `lastSuccessfulCheck`.
- Failed automatic checks can retry on the next invocation.
- Candidate-to-version assignments are bounded to the newest 100 entries.
- Corrupt state is renamed to a timestamped backup and treated as empty, with
  a warning.
- State is advisory. Installed receipts remain the authority for whether a
  candidate already exists.

## CLI

Extend `Action` with:

```text
CheckUpdate
Update
AutoUpdate
SelfUpdate
```

### `CheckUpdate`

```bat
pkg --action CheckUpdate C:\Packages\Tool
```

- resolves the active version
- ignores the fixed due-check timestamp
- performs discovery only
- does not download the application payload
- does not create a version or update `current`
- records successful check state
- exits `0` for both `current` and `available`

Human output names the current version, candidate version, and candidate ID.
`--toml` emits one stable TOML result document for automation.

### `Update`

```bat
pkg --action Update C:\Packages\Tool
```

- explicitly checks regardless of the fixed due-check timestamp
- prepares and activates an available update
- does not require `allow_automatic_update`
- is interactive only when a future feature explicitly adds confirmation
- succeeds without changes when already current

`--check-only` is not added because `CheckUpdate` already expresses that
operation.

### `AutoUpdate`

```bat
pkg --action AutoUpdate C:\Packages\Tool
```

- checks when the fixed internal interval says it is due
- reports but does not apply when `allow_automatic_update = false`
- prepares and activates when `allow_automatic_update = true`
- honors version-owned `ignore_checksum` and an explicit `--no-checksum`
  invocation
- never pauses or prompts

This action is suitable for callers that already have their own automation.
`pkg` does not create, configure, or remove that automation.

### Automatic checks during Install

When Install targets a package root or `current`:

1. install/repair the explicitly selected version first
2. run a due check after successful repair
3. report an available update
4. apply it only when `allow_automatic_update = true`

Doing the check after repair preserves the existing Install contract when the
network is unavailable. A failed automatic check produces a warning, not a
failed repair. If automatic apply begins and fails, Install reports the update
failure but leaves the repaired old version active.

Explicit `Update` remains fail-fast because update is the requested operation.

### Checksum policy

`--no-checksum` remains a supported CLI option for Install, Update,
AutoUpdate, and SelfUpdate.

For each downloaded payload, apply this precedence:

1. `--no-checksum` present: skip verification and record `cli-bypass`.
2. Version config has `ignore_checksum = true`: skip verification and record
   `version-ignore`.
3. Otherwise: require a valid SHA-256, verify it, and record `verified`.

Git payloads do not use archive checksums and record `not-applicable`.

A checksum mismatch fails unless one of the two bypasses was selected before
verification. `pkg` must not respond to a mismatch by silently falling back to
unverified installation.

Every bypass prints a warning containing the candidate version and the source
of the bypass. Secrets and signed URL query strings remain redacted.

### Exit codes

Keep the existing meanings:

- `0`: requested action completed, including “already current”
- `2`: configuration, candidate, or user-input error
- `3`: network, download, unpack, filesystem, Git, or activation failure
- `4`: unexpected internal error

Update availability is data, not an error code. Automation should use `--toml`
rather than interpreting console prose.

## Detailed update workflow

### 1. Resolve and validate

1. Resolve the input to a package root and active concrete version.
2. Require a valid `current` junction for automatic and update actions.
3. Load and normalize `pkg.toml`.
4. Validate `[update]`, referenced modules, and current update receipt.
5. Reject updates from an explicit non-current version unless a future
   `--from-version` option is added.

### 2. Acquire the package lock

Create:

```text
<package-root>/.pkg/locks/update.toml
```

The TOML lock uses exclusive creation and records process ID, host, action, and
start time. A second process fails clearly without checking or mutating state.

After acquiring the lock, create
`.pkg/work/<operation-id>/pycache/` before loading any package-local module.
Even a check-only action gets a short-lived operation directory so module
imports cannot write bytecode into the active version.

Do not automatically delete a lock merely because it is old. Add a separate
future `--break-update-lock` recovery option that verifies the recorded process
is no longer running before removal.

Read-only `CheckUpdate` also takes the lock because it writes check state and
candidate timestamp assignments.

### 3. Check

1. Run the built-in Git checker or call the configured Python check module.
2. Normalize its result to `current` or `available`.
3. Validate candidate ID, version, URL, checksum, and metadata.
4. Compare the candidate with the current version/receipt.
5. Search `.pkg/receipts/*.toml` for the candidate ID.
6. Persist the check result atomically.

If the candidate is already installed but not active, Update activates that
existing version instead of downloading it again.

### 4. Expand the operation work directory

When a candidate will be prepared, add the remaining work directories:

```text
<package-root>/.pkg/work/<operation-id>/
  download/
  extract/
  version/
  pycache/
  receipt.toml
```

Resolve every work path and verify it remains under
`<package-root>/.pkg/work/<operation-id>` before recursive cleanup.

While importing anything from `pkg.local/`, redirect or disable Python
bytecode generation so `__pycache__/` and `.pyc` files can exist only under
this operation's `pycache/` directory. Importing a check, unpack, origin, or
helper module must not write into a concrete version directory.

### 5. Build the version skeleton

Copy the current version into `version/`, excluding `App/`, `__pycache__/`, and
`.pyc` files. Do not follow junctions or symlinks while copying. Reject reparse
points in the skeleton until a specific safe use case requires them.

Synchronize the staged `pkg.toml` top-level fields:

```toml
version = "<candidate upstream version>"
localVersion = <assigned local revision>
```

Apply the `[origin]` reconciliation rules before payload acquisition so invalid
config fails early.

### 6. Acquire and populate `App/`

Git mode:

1. initialize or clone into staged `App/`
2. fetch the configured ref
3. verify the exact candidate commit exists
4. check out that exact commit in detached HEAD state
5. configure the expected remote URL without embedding credentials

Archive mode:

1. download to `download/<safe-file-name>.part`
2. enforce redirect, timeout, and maximum-size policy
3. stream SHA-256 calculation during download
4. compare the checksum before renaming the artifact unless version policy or
   `--no-checksum` explicitly bypasses verification
5. safely extract into a second temporary directory
6. copy the selected subdirectory into staged `App/`

Module unpack mode:

1. perform the same built-in verified download
2. load and call the configured unpack module
3. verify staged `App/` after the module returns

No mode writes into the old version's `App/`.

### 7. Validate the candidate

Before exposing the version:

1. require non-empty `App/`
2. parse and normalize staged `pkg.toml`
3. require metadata to match the assigned directory name
4. validate origin and update module references
5. run the same observable checks as `HealthCheck`
6. reject manager artifacts, partial files, bytecode caches, and work
   directories anywhere in the staged version
7. write the candidate receipt to
   `.pkg/work/<operation-id>/receipt.toml`
8. optionally run a configured future application health command only after a
   separate command-execution design is approved

### 8. Commit the version

Move:

```text
<package-root>/.pkg/work/<operation-id>/version
    -> <package-root>/v<version>.l<revision>
```

with one same-volume atomic rename.

The destination must not already exist. An existing destination is reconciled
by its root-owned receipt before this step; it is never overwritten.

After the version rename succeeds, atomically move:

```text
<package-root>/.pkg/work/<operation-id>/receipt.toml
    -> <package-root>/.pkg/receipts/<version-directory-name>.toml
```

If the process stops between those two renames, the next action detects the
final version plus the pending work receipt, validates both, and completes the
receipt move. It never writes recovery markers into the version directory.

### 9. Activate through Install

Call the existing high-level Install workflow on the new concrete version.
That workflow:

- updates `current`
- skips origin population because staged `App/` is non-empty
- repairs shortcuts
- writes environment variables and PATH entries
- recreates wrappers

Update must pass neither `--refresh-app` nor `--force`. Version ordering and
the candidate validation already establish that the new version is eligible.

### 10. Roll back activation on failure

Record the prior `current` target before activation.

If component installation fails after `current` changes:

1. atomically restore `current` to the recorded old version
2. run Install for the old version to repair external components
3. retain the prepared new version for diagnosis and retry
4. mark the receipt activation status as failed in manager-owned state
5. report both the original failure and any rollback failure

Never delete the old version during update. Old-version cleanup is a separate,
explicit future action.

### 11. Cleanup

On success or handled failure, remove only the resolved
`.pkg/work/<operation-id>/` directory. Release the lock last.
Check-only actions perform the same cleanup immediately after persisting their
result.

If cleanup fails, preserve the primary action result and emit a warning naming
the exact leftover path.

## Download and extraction safety

Reuse and generalize the existing origin download and safe zip extraction
behavior rather than implementing a second archive stack.

Required rules:

- accept only HTTP and HTTPS
- reject URLs containing user-info credentials
- use finite connect/read timeouts
- cap redirects
- reject redirects to non-HTTP(S) schemes
- optionally cap download size with `[update.payload].maxSizeMB`
- default `maxSizeMB` to a documented conservative value such as 2048
- download only under `.pkg/work/<operation-id>/download/` with a `.part`
  suffix
- require SHA-256 before extraction unless version policy or `--no-checksum`
  explicitly bypasses verification
- reject absolute archive paths, drive prefixes, `..`, symlinks, hard links,
  and unsafe reparse-point representations
- never extract directly into the final version or installed `App/`
- validate `extractSubdir` after extraction and before copying
- remove partial downloads only inside the verified operation work directory

Network errors must not advance `lastSuccessfulCheck` and must never alter
`current`.

## Self-update

### Required installed layout

Self-update is supported only from a bootstrapped layout:

```text
Pkg/
  pkg.cmd                 # stable launcher, not version-owned
  pkg.python              # optional stable interpreter selection
  current/                # junction to active pkg version
  .pkg/
  v0.12.0.l1/
    pkg.toml
    App/
      pkg.py
      install.cmd
      install-machine.cmd
      update-config.cmd
      ...
```

The stable launcher:

1. resolves its own directory as `PKG_HOME`
2. selects Python using the existing precedence rules
3. invokes `%PKG_HOME%\current\App\pkg.py`
4. forwards all arguments unchanged

The self-package's application payload is the released `pkg` source bundle.
This lets ordinary archive preparation populate staged `App/` and keeps the
version root available for package metadata. `pkg.py` uses `PKG_HOME` only to
recognize a self-managed installation. A missing or inconsistent `PKG_HOME`
makes `SelfUpdate` fail with guidance. A developer checkout must never update
itself in place.

### Self-update configuration

The `pkg` package uses an ordinary local check module configured in its own
`pkg.toml`. That versioned module determines the newest official release and
must return:

- version
- immutable archive URL
- SHA-256 unless version policy or `--no-checksum` bypasses it
- candidate ID
- minimum bootstrap protocol version

Self-update always requires HTTPS. It requires SHA-256 unless the installed
`pkg` version declares `ignore_checksum = true` or the invocation uses
`--no-checksum`. Signature verification should be added before enabling
self-update by default for broad distribution; a checksum protects integrity
only when the release source used by the check module is trusted.

### Self-update sequence

```bat
pkg --action SelfUpdate
```

1. resolve `PKG_HOME/current` and verify it matches the running `pkg.py`
2. load the local check module and perform an explicit release check
3. reject an incompatible `minimumBootstrapVersion`
4. prepare the new version with the ordinary staged update workflow
5. run the staged `App/pkg.py --version`
6. run staged HealthCheck against its package config
7. atomically move the version into `PKG_HOME`
8. repoint `current`
9. launch the new version with an internal `--complete-self-update` health
   action
10. restore the old junction if the new process fails

The stable launcher is not replaced during normal self-update. Bootstrap
launcher changes require an explicit, separately designed bootstrap upgrade,
which avoids overwriting a batch file that is currently executing.

Because Python has already loaded the running source, switching the junction
does not overwrite or delete running files. The old version remains available
for rollback.

### Automatic self-update

There is no hidden self-update check and no built-in scheduling. An external
caller may invoke `AutoUpdate` for the self-package; it may apply the candidate
only when that installed version declares
`allow_automatic_update = true`.

## Logging and TOML output

Human output should make the phase visible:

```text
Checking Tool for updates...
Current: v2.3.0.l1
Available: v2.4.0.l1 (release:2.4.0)
Downloading 84.2 MB...
Verifying sha256...
Preparing v2.4.0.l1...
Health check passed.
Activating v2.4.0.l1...
Update completed; previous version remains at v2.3.0.l1.
```

Never log:

- HTTP authorization headers
- environment variables
- URLs containing credentials
- signed URL query strings
- module context values containing future secret fields

`--toml` writes one TOML document to stdout. Progress and diagnostics go to stderr so
callers can parse stdout reliably:

```toml
ok = true
status = "updated"
changed = true
package = "Tool"
previousVersion = "v2.3.0.l1"
currentVersion = "v2.4.0.l1"
candidateId = "release:2.4.0"
warnings = []
errors = []
```

Stable statuses in protocol version 1:

- `not-configured`
- `not-due`
- `current`
- `available`
- `prepared`
- `updated`
- `rolled-back`
- `failed`

## Failure behavior

| Failure point | Active version | Candidate files | Result |
|---|---|---|---|
| check fails | unchanged | none | warning for implicit check; error for explicit action |
| download fails | unchanged | staging only | staging cleaned, retry allowed |
| checksum fails | unchanged | staging only | hard failure, never unpack |
| unpack module fails | unchanged | staging only | hard failure, diagnostics retained in output |
| staged health check fails | unchanged | staging only | hard failure, never creates final version |
| final version rename fails | unchanged | staging may remain | hard failure with exact recovery path |
| activation/install fails | restored to old version | new final version retained | rollback and report |
| rollback fails | explicitly reported | both versions retained | mutation error requiring manual repair |

Automatic update failure must never be disguised as success. When it occurs as
an optional post-Install action, the Install result states that repair
succeeded but automatic update failed.

## Internal implementation shape

Keep runtime behavior in `src/pkg.py`, consistent with the repository's current
single-file direction.

Suggested high-level functions:

```python
check_package_update(package_path, *, ignore_interval)
update_package(package_path, *, automatic)
enable_auto_update(package_path, scope)
disable_auto_update(package_path, scope)
self_update()
```

Suggested concrete helpers:

```python
normalize_update_config(raw_update)
check_git_update(identity, update_config, state)
load_package_local_module(identity, module_path)
check_module_update(identity, update_config, state)
normalize_update_candidate(raw_candidate, identity, mode)
prepare_update(identity, runtime_config, candidate)
prepare_git_app(stage_app, candidate)
download_update_artifact(stage_root, candidate, payload_config)
prepare_zip_app(stage_app, artifact, payload_config, candidate)
call_update_unpack_module(identity, stage_root, candidate, payload_config)
write_update_receipt(work_dir, final_version_name, candidate)
activate_prepared_version(old_identity, new_identity, scope)
```

This is guidance, not a required private API. Helpers should name real
side-effect boundaries or concepts. Do not create:

- checker/provider base classes
- plugin registries
- a general pipeline engine
- a forest of candidate/config dataclasses
- tests that assert these helper names or call order

The public coordinators should use direct, narrated blocks in this order:

```text
resolve -> validate -> lock -> check -> stage -> validate -> commit -> install
```

Reuse existing `PackageIdentity`, `StepResult`, `ActionResult`, atomic writes,
version comparison, junction management, origin zip safety, config
normalization, and Install behavior.

## Test design

Follow `docs/tests.md`: protect observable behavior and mock only real
boundaries such as clocks, network, Git subprocesses, and self-update
smoke-test processes. Package-local modules run in-process and are tested
through public actions.

### Configuration behavior

Protect:

- Git, module-check, zip, and module-unpack configurations are
  accepted
- unknown keys and invalid mode combinations fail clearly
- module paths outside `pkg.local/` are rejected
- conventional module names work without explicit paths
- explicitly configured and helper module names below `pkg.local/` are allowed
- non-boolean `allow_automatic_update` is rejected
- unsafe relative paths are rejected
- HealthCheck validates update configuration and module references without
  checking the network

### Git behavior

Protect:

- equal local and remote commits report `current`
- a different remote commit reports `available`
- Git versions use a UTC `YYYYMMDD-HHMMSS-git` upstream version
- repeated checks of one commit reuse the assigned version
- update checks do not mutate the installed checkout
- apply checks out the exact checked candidate commit
- a timestamp collision increments `.lN`
- an already installed candidate activates without another clone

Use a local bare Git repository as the real Git boundary where practical.
Mock the clock. Do not assert private Git helper calls.

### Module check and download behavior

Protect:

- a module result with a newer version reports `available`
- equal versions report `current` only for the same candidate
- republished equal versions fail closed
- apply downloads, verifies, extracts, and installs the candidate
- checksum mismatch leaves the old version active
- unsafe zip paths leave the old version active
- `extractSubdir` selects the intended App contents
- download or extraction failure leaves no final version directory
- missing checksum fails when neither bypass is active
- version-owned `ignore_checksum = true` allows an unverified download and
  records `version-ignore`
- `--no-checksum` allows an unverified download and records `cli-bypass`

Mock HTTP at the network boundary. Do not use live internet services.

### Module contracts

Protect:

- check modules receive the documented Python context
- `None` and valid candidate mapping results are accepted
- import errors, API mismatches, exceptions, and invalid mappings fail visibly
- unpack modules receive only staging paths
- a successful unpack must leave a non-empty staged `App/`
- modules cannot be configured outside `pkg.local/`
- relative imports can use package-local helper modules with arbitrary names

Use local fixture modules. Test the observable CheckUpdate and Update results,
not `importlib` call order or internal module namespace names.

### Staging and activation

Protect:

- the old `App/` and old version are never changed during preparation
- partial downloads, extraction trees, caches, and receipts stay under
  package-root `.pkg/`
- importing `pkg.local` modules does not create `__pycache__/` or `.pyc` files
  in a version directory
- a finalized version contains no locks, state, receipts, partial files, work
  directories, or manager-generated temporary files
- staged config metadata matches the new directory
- a failed staged HealthCheck never exposes a final version
- successful apply creates a version then activates it
- component failure restores the old `current`
- retry reuses an already prepared candidate safely
- concurrent Update actions do not both prepare or activate

### Automatic policy

Protect:

- explicit checks ignore the fixed due-check timestamp
- automatic checks respect the fixed internal interval
- failed checks do not advance the successful-check timestamp
- an explicit old-version Install does not auto-check
- root/current Install checks only after successful repair
- `allow_automatic_update = false` reports but does not download
- `allow_automatic_update = true` applies without prompting

### Self-update

Protect:

- SelfUpdate refuses a developer checkout
- SelfUpdate requires a stable launcher layout
- the running version is not overwritten
- staged `pkg.py --version` failure leaves old `current`
- successful self-update switches `current`
- post-switch failure restores old `current`
- incompatible bootstrap versions fail before activation

Use a temporary fake self-install tree and subprocess boundary. Do not change
the developer's real launcher.

### Documentation constraints

Do not add tests for:

- exact private helper names
- the physical location of implementation sections in `pkg.py`
- this blueprint's Markdown structure
- exact TOML comments in generated examples unless the text is a user-visible
  CLI contract

## Documentation work

When implementing:

1. Update `README.md` with Git, downloadable zip, custom check, and custom unpack
   examples.
2. Add a concise update state/layout section.
3. Extend `--help-extended` with the public actions and policy semantics.
4. Document automatic checks as post-repair behavior.
5. Document that imported package-local modules are trusted and not sandboxed.
6. Document self-update's stable-launcher prerequisite.
7. Update `docs/development_guide.md` with the three-phase update coordinator.
8. Add fixture examples under `tests/fixtures/`, not production example
   packages, unless a real maintained package adopts the feature.

## Implementation plan

### Phase 1: Normalize update configuration

- add `[update]`, `[update.check]`, and `[update.payload]`
- add HealthCheck validation
- add `pkg.local/` path validation and conventional module resolution
- add TOML result rendering without changing actions yet

Exit criterion: valid recipes normalize to small canonical dictionaries and
invalid combinations fail without network or filesystem mutation.

### Phase 2: Read-only checks

- implement Git and Python module check modes
- add `CheckUpdate`
- add candidate normalization
- add atomic state and candidate timestamp assignment
- add package-root update locking

Exit criterion: all check modes reliably report `current` or `available`
without modifying installed version contents or `current`.

### Phase 3: Staged payload preparation

- build version skeletons
- create the exact `.pkg/work`, `.pkg/locks`, `.pkg/state`, `.pkg/receipts`,
  `.pkg/logs`, and `.pkg/backups` layout
- redirect module bytecode caches away from version directories
- implement verified downloads
- reuse safe zip extraction
- implement exact Git checkout
- implement Python unpack module contract
- reconcile `[origin]`
- validate and write receipts

Exit criterion: a candidate can become a complete validated staging tree, but
no action activates it yet.

### Phase 4: Activation and rollback

- add `Update`
- atomically commit staged versions
- delegate activation to Install
- restore the old version on component failure
- make retry and already-installed candidate behavior idempotent

Exit criterion: explicit updates either finish on the new version or leave the
old version active and repairable.

### Phase 5: Automatic policy

- add the fixed internal due-check interval
- add post-Install checks
- add `AutoUpdate`
- implement consistent checksum policy for required, version-ignore, and
  CLI-bypass cases
- add `--toml`

Exit criterion: automatic application is controlled by one boolean, checks use
fixed internal timing, and no scheduling facility is introduced.

### Phase 6: Stable bootstrap and self-update

- change installation layout to a stable root launcher plus versioned `pkg`
- add `PKG_HOME` validation
- define official self-update module result requirements
- add staged self-health checks and rollback
- add `SelfUpdate`

Exit criterion: `pkg` can update to a new version without writing into or
deleting the running version.

## Acceptance criteria

The feature is complete when:

1. `pkg.toml` supports strict update policy, checker, and payload tables.
2. Git-backed `App/` packages can detect a changed remote ref without mutating
   the installed checkout.
3. Git-backed updates use UTC `YYYYMMDD-HHMMSS-git` upstream versions and exact
   commit candidate IDs.
4. Package-local check modules can detect a downloadable version that `pkg`
   downloads, applies the declared checksum policy to, and safely extracts.
5. Custom Python check, unpack, and origin modules use the documented
   in-process API and live under `pkg.local/`.
6. Downloads, extraction, and unpack modules populate only staging before
   activation.
7. Every installed update has an exact, credential-free receipt under
   package-root `.pkg/receipts/`, never inside the version.
8. Rechecking or retrying one candidate is idempotent.
9. Failed preparation leaves the old version and `current` unchanged.
10. Failed activation restores the old version and repairs its components.
11. `allow_automatic_update` is the only configurable automatic policy.
12. `pkg` provides no scheduled-task or background-job functionality.
13. Existing Install, UpdateConfig, HealthCheck, and origin population behavior
    remain compatible.
14. Self-update works only through a stable launcher, never overwrites the
    running version, and can roll back the junction.
15. Permanent tests cover user-visible behavior without depending on private
    helper layout.
16. No temporary, work, state, lock, receipt, log, bytecode-cache, or partial
    download file is ever committed into a final version directory.

## Decisions closed by this blueprint

- Update checking and exact-version origin population remain distinct.
- Git discovery is built in; downloadable-version discovery uses a local
  Python module.
- Package-authored hooks are imported Python modules, not executed scripts or
  arbitrary executable types.
- Package-local modules live under `pkg.local/`.
- `check_update.py`, `unpack_app.py`, and `populate_app.py` are recognized
  defaults, while other module and helper names are allowed.
- Module APIs use in-memory Python values and start at API version 1.
- Persistent state, receipts, configuration, and machine-readable CLI output
  use TOML.
- All manager-generated files live in precise subdirectories of package-root
  `.pkg/`; `/.pkg/` is sufficient to ignore them.
- Final version directories never contain temporary or manager-control files.
- Root/current installs perform checks on a fixed internal interval.
- `allow_automatic_update` is the only configurable automatic policy.
- `pkg` does not create or manage scheduled tasks or background jobs.
- Git update checks do not fetch into the installed checkout.
- Git version timestamps use UTC.
- Candidate IDs, not display versions, provide idempotency.
- All application payloads are prepared in package-root `.pkg/work/`.
- SHA-256 is required unless the version declares `ignore_checksum = true` or
  the invocation explicitly uses `--no-checksum`.
- Receipts distinguish verified downloads, version-owned checksum exceptions,
  and CLI checksum bypasses.
- New versions are never overwritten and old versions are not automatically
  deleted.
- Self-update requires a stable launcher outside version directories.
- The implementation stays concrete inside `src/pkg.py`; no provider framework
  is introduced.
