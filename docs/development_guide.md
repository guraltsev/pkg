# Development architecture

`src/pkg/pkg.py` is the stable executable and public Python facade. It contains CLI
dispatch and the high-level command workflows, so a maintainer can read an
install or update from validation through its final result without following a
generic pipeline. Significant implementation domains live directly in
`src/pkg/`.

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

The facade imports only the names needed by its workflows. It does not provide
compatibility re-exports, a provider framework, or a configurable install
pipeline. New implementation code should be placed in the module that owns its
state or side effect. Legacy format conversion remains implemented in
`legacy_to_pkg_toml.py`; `pkg config from-legacy` coordinates its public CLI
result.

## Update coordinator

The public update coordinator in `src/pkg/pkg.py` resolves the active package,
validates its `[update]` table, acquires the package-root lock, checks for a
candidate, asks the `updates` module to stage a complete version under
`.pkg/work`, and atomically commits it. A separate explicit `pkg upgrade
install` command activates the downloaded version through the normal install
workflow. Check and unpack hooks are imported from `pkg.local/` as trusted
in-process Python extensions; they are never executed as shell commands.

Git payloads, including bootstrap `vbootstrap-git.l1` packages, create a new
timestamped version directory. The update model has no mutable in-place mode
and no automatic update policy.

Installing a normal Git-backed `vbootstrap-git.l1` template enters the update coordinator
before origin population or junction management. The resolved commit is staged
directly into the timestamped version, so `vbootstrap-git.l1` never becomes an installed
payload.

The same coordinator accepts a `vbootstrap.l1` template backed by a trusted
module check and ZIP or module payload. Release discovery remains
package-specific while bootstrap staging stays generic.

A Git origin defaults to `refs/heads/main` and supplies the default Git update
check, so package metadata needs only one source URL and ref. Explicit update
checks remain available for checkout-path or remote-name customization.

Update work, locks, timing state, and receipts are manager-owned data beneath
the package root's `.pkg/` directory. A finalized version directory contains
only package-authored files and its completed `App/` payload.
