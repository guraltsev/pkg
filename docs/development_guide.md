# Development architecture

`src/gupkg/cli.py` is the sole command-line boundary: it owns grammar, one-time
context resolution, dispatch, rendering, and exit translation. `gupkg.py` owns
public single-package workflows and has no CLI compatibility entrypoint.
Significant implementation domains live directly in `src/gupkg/`.

The runtime package has one-way domain boundaries:

- `core` provides dependency-light models and utilities.
- `windows` isolates operating-system integration.
- `layout` resolves package identities and activates `current`.
- `configuration` normalizes and validates the canonical TOML schema.
- `metadata` synchronizes directory-owned TOML fields without rewriting
  unrelated content.
- `components` applies shortcuts, environment variables, `PATH`, and wrappers.
- `origin` prepares and replaces `App/` payloads.
- `updates` owns state, hook loading, candidate normalization, and staging.
- `registry` owns the sparse Git checkout/cache lifecycle and validates registry
  data without importing package-local code.
- `distribution` owns standalone runtime health, bootstrap metadata checks, and
  scoped self-repair.

The CLI imports only the names needed by its workflows. It does not provide
compatibility aliases or a provider framework. Update payload preparation
supports a small declarative sequence of built-in payload and trusted
package-local module steps; ordinary component installation remains fixed. New
implementation code should be placed in the module that owns its state or side
effect. Legacy format conversion remains implemented in
`legacy_to_gupkg_toml.py`; `gupkg config-fix` coordinates its explicit public
conversion result.

## Manager integration

Manager mode is an orchestration layer over collection discovery and the
existing single-package operations. `manager.py` owns configuration validation,
scoped inventory, planning, and the batch executor; the CLI and manager TUI
must call those same plan/executor boundaries. Planning may check providers and
update package-owned check state, but it must not download or activate a
version. The executor revalidates ownership and health before each target and
records skipped, failed, upgraded, and not-attempted outcomes.

The TUI deliberately keeps planning, confirmation, and execution on separate
scrollable screens. Worker tasks must leave Textual's event loop free while
provider and package operations run. Cancellation is a boundary request: it
prevents another target from being scheduled and never interrupts an operation
already in progress. Mixed-scope elevation is resolved before invoking the
executor. Per-target activation passes the manager installation context, so
wrappers land in the configured `[bin]` directories rather than the scope
defaults. Update checks capture process-wide stdout and therefore run one at a
time. Manual Windows coverage for UAC, missing roots, duplicate selectors,
partial failures, and terminal dimensions lives in `tests/manual_smoke.md`.

Manager schema v2 adds separate package roots, wrapper/bin roots, and a
registry cache. Configuration discovery and migration are implemented in the
manager layer; the operational contract is documented in
`docs/operations.md`. Registry selectors are resolved only after the active
cache has passed manifest and path validation.

## Update coordinator

The public update coordinator in `src/gupkg/gupkg.py` resolves the active package,
validates its `[update]` table, acquires the package-root lock, checks for a
candidate, asks the `updates` module to stage a complete version under
`.gupkg/work`, and atomically commits it. Check, download, and full update share
that one locked session. `gupkg update --download-only` leaves the staged
version inactive with a pending receipt; a full `gupkg update` reuses a version
whose receipt is still pending and activates it. Workflows return errors in
their `ActionResult` rather than printing them; the CLI and TUIs render them. Check and
unpack hooks are imported from `pkg.local/` as trusted
in-process Python extensions; they are never executed as shell commands.

Git payloads, including bootstrap `vbootstrap-git` packages, create a new
timestamped version directory. The update model has no mutable in-place mode
and no automatic update policy.

Installing a normal Git-backed `vbootstrap-git` template enters the update coordinator
before origin population or junction management. The resolved commit is staged
directly into the timestamped version, so `vbootstrap-git` never becomes an installed
payload.

The same coordinator accepts a `vbootstrap` template backed by a trusted
module check and ZIP or module payload. Release discovery remains
package-specific while bootstrap staging stays generic.

A Git origin defaults to `refs/heads/main` and supplies the default Git update
check, so package metadata needs only one source URL and ref. Explicit update
checks remain available for checkout-path or remote-name customization.

Update work, locks, timing state, and receipts are manager-owned data beneath
the package root's `.gupkg/` directory. A finalized version directory contains
only package-authored files and its completed `App/` payload.

Standalone release assembly is intentionally separate from runtime startup.
`tools/build_standalone.py` receives an explicit, digest-verified embedded
runtime and creates the versioned payload; release automation supplies the
outer bootstrap executable. This keeps source checkouts from becoming an
implicit runtime dependency and makes runtime provenance auditable.
