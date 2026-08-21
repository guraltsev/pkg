# Job 04: Upgrade-all planner, executor, and CLI

Status: Closed — implemented by commit `6e50ba2`.

Depends on: Job 03

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Implement the safe batch-upgrade domain and noninteractive/console workflow.
At the end of this job, `gupkg upgrade all` can plan, confirm, and sequentially
upgrade every eligible installed target while truthfully reporting skips,
partial failures, and exit status.

Do not add the interactive manager Upgrade All screens in this job; Job 05
will consume the completed batch API.

## Required preparation

Read the repository instructions and complete issue 011. Inspect
`check_package_update`, `full_package_upgrade`, package-root locks, receipts,
elevation helpers, and the managed target/result models delivered earlier.

## Work

1. Add a result-oriented upgrade plan over a fresh managed inventory.
2. A target is eligible only when it is in the selected complete scope,
   installed through valid `current`, healthy, update-configured, and reported
   to have an available candidate.
3. Record distinct skip reasons for uninstalled, broken, unhealthy,
   not-configured, current, incomplete, and failed-check targets. Historical
   manifests may be diagnosed, but batch mutation runs once per installed
   target.
4. Implement:

   ```text
   gupkg upgrade all [--scope user|system|all]
                     [--yes]
                     [--dry-run]
                     [--fail-fast]
                     [--local-deps-autoinstall]
                     [--toml]
   ```

5. Interactive console use prints the complete plan and requests one
   confirmation. Noninteractive use refuses mutation without `--yes`.
   `--dry-run` may refresh package-owned update-check state, but it must not
   download, activate, elevate, or change installation components.
6. Revalidate target path ownership, configured scope, `current`, and health
   immediately before each mutation. Call the existing single-package full
   upgrade operation; do not copy update/install behavior into the batch.
7. Execute sequentially in deterministic user-then-system selector order.
   Continue after failures by default; `--fail-fast` marks later eligible
   targets not attempted.
8. Use existing package-root locks and recovery state. Do not add global
   rollback or pretend the batch is atomic.
9. Preflight elevation before any mutation. If system targets are planned and
   the process is not elevated:

   - interactive console use may relaunch the entire confirmed argument vector
     through the existing Windows elevation boundary;
   - noninteractive use fails with an actionable exit-code-3 message;
   - no user target may be changed before this decision; and
   - arguments must be passed as a vector, never reconstructed as shell text.

10. Render complete human and TOML summaries from one aggregate result. TOML
    stdout must contain per-target outcomes and totals without progress logs.
11. Return the existing most-severe exit category after all scheduled targets
    have completed.

If importing existing package operations creates a cycle, perform the smallest
cohesive extraction of public operations needed to remove it. Do not combine
this work with an unrelated CLI rewrite.

## Observable behavior to protect

Add permanent integration-level tests for:

- only healthy installed targets with available candidates being mutated;
- every ineligible state receiving its documented skip reason;
- deterministic user-then-system order;
- full package operations receiving the configured explicit scope;
- revalidation detecting changed or escaped targets before mutation;
- continuation after failure and truthful partial-success summary;
- fail-fast marking later work not attempted;
- safe retry after partial success;
- package locks continuing to reject concurrent update work;
- console and noninteractive confirmation rules;
- dry-run causing no download, activation, elevation, or component changes;
- mixed-scope elevation occurring before any mutation;
- declined/failed elevation causing no mutation;
- no attempted cross-package rollback; and
- most-severe exit status and parseable TOML output.

Mock provider, elevation, subprocess, registry, and installation boundaries as
needed. Exercise real manager and package directory layouts. Assert observable
order and outcomes, not private helper calls.

## Completion criteria

- `upgrade all` satisfies the issue 011 eligibility and safety contract.
- Rerunning after partial success is safe.
- No uninstalled package can enter the mutation set.
- Existing single-package upgrade behavior and locks remain authoritative.
- Focused integration tests and the full test suite pass.
- CLI help documents confirmation, dry-run, elevation, and partial failure.
- `git diff --check` reports no whitespace errors.
