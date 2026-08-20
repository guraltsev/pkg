# Job 05: Batch-upgrade TUI, documentation, and release hardening

Depends on: Job 04

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Complete manager mode by exposing the proven batch engine in the TUI, updating
user/developer documentation, and validating the full workflow across user and
system roots. This is the integration and release-readiness job, not an
opportunity for unrelated refactoring.

## Required preparation

Read the repository instructions, issue 011, `docs/tui_style_guide.md`, and all
prior job implementations. Run the full test suite before editing so existing
failures are distinguished from regressions.

## Work

1. Enable `Upgrade all installed packages` on the manager home.
2. Implement the two-stage TUI flow from issue 011:

   - a non-installing planning/result screen with available, current, skipped,
     and failed-check counts;
   - a confirmation/settings screen with `Run planned upgrades` first;
   - visible scope, checksum, dependency-auto-install, and fail-fast settings;
   - a responsive scrollable execution result with per-target state and final
     totals.

3. Use the Job 04 plan/executor directly. Do not create separate TUI batch
   semantics or infer success from captured log strings.
4. Handle mixed-scope elevation before mutation. Declining or failed elevation
   returns to a truthful result without changing user packages.
5. Refresh inventory and installed versions after completion or partial
   completion. Cancellation stops new scheduling only at the safe boundaries
   defined by the batch API.
6. Update README and relevant development documentation with:

   - central installation and retained package-local mode;
   - the exact `gupkg-config.toml` schema;
   - environment and relative path rules;
   - root ownership and non-nesting rules;
   - CLI and TUI manager workflows;
   - list, Doctor, selection, and update status semantics;
   - confirmation, dry-run, elevation, locking, and partial-failure behavior;
   - recovery by fixing the failed target and safely rerunning; and
   - a migration checklist that does not require package content changes.

7. Update CLI extended help and documentation indexes where appropriate.
8. Review all issue 011 acceptance criteria and close gaps with behavior-focused
   tests. Avoid tests that merely verify documentation text or source layout.
9. Run manual smoke scenarios for:

   - user-only and system-only inventories;
   - duplicate selectors in both roots;
   - a missing configured root;
   - valid, absent, and broken `current`;
   - bootstrap promotion;
   - mixed-scope elevation accepted and declined;
   - one package failing during a continuing batch;
   - safe rerun after partial success;
   - narrow and short terminal sizes; and
   - human and TOML console output.

Record any manual steps that are important for future releases in the existing
manual smoke documentation rather than in implementation comments.

## Observable behavior to protect

Add only the remaining durable TUI/integration coverage:

- opening Upgrade All performs planning but not installation;
- confirming invokes the same batch contract as the CLI;
- the terminal stays responsive during checks and mutations;
- per-target progress and final partial-failure summaries remain accessible;
- elevation happens before any mutation;
- cancellation does not begin another package operation;
- returning to the browser shows newly installed versions; and
- package-local and ad-hoc collection modes remain available.

Do not add screenshot snapshots, exact output-spacing assertions, README text
tests, or implementation-shape tests.

## Completion criteria

- Every acceptance criterion in issue 011 is implemented or an explicit,
  documented blocker is reported.
- The manager TUI supports browsing, individual operations, update refresh,
  Doctor, and confirmed Upgrade All.
- User documentation is sufficient to create a manager file and recover from a
  partial batch without reading source code.
- The full test suite passes.
- Manual smoke results cover Windows scope/elevation behavior that unit tests
  cannot establish reliably.
- `git diff --check` reports no whitespace errors.

