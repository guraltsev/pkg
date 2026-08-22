# Job 06: Manager-mode acceptance coverage and hardening

Status: Closed — implemented by commit `09e4cb0`.

Validation: `121 passed, 8 subtests passed`; `git diff --check` passed.

Depends on: Jobs 01-05

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Bring the implemented manager mode to release confidence. Verify every
important observable manager workflow through its public CLI or TUI boundary,
using real temporary manager/package layouts and mocks only for external
boundaries. Fix implementation defects that those tests expose. This is one
end-to-end hardening job: do not split CLI, batch safety, and manager-TUI
coverage into separate jobs because they share the same managed inventory,
scope, and upgrade contract.

The current suite passes but is insufficient: it covers only a small subset of
manager CLI dispatch, directly unit-tests the planner with synthetic objects,
and contains no manager-TUI coverage.

## Required preparation

Read and follow:

- `AGENTS.md`
- `docs/tests.md`
- `docs/docstring_schema.md`
- `docs/python_rules.md`
- `docs/tui_style_guide.md`
- the complete issue 011 design, especially its test strategy and acceptance
  criteria

Run the full suite before editing and record the baseline. Review the existing
manager tests before adding coverage; retain tests that protect genuine public
behavior, but replace implementation-coupled synthetic tests where a real
public-boundary test can cover the behavior better.

## Work

1. Add manager CLI integration coverage using `main(argv)` with real temporary
   manager files and package directories. Mock only providers, elevation,
   registry/subprocess work, and other true external boundaries.

   Cover explicit `--config`, package-path precedence, both locked scopes,
   duplicate selection, all list filters, Doctor diagnostics and its
   no-network/no-mutation contract, parseable TOML outputs, and aggregate exit
   status.

2. Cover the complete batch-upgrade contract through the manager CLI with real
   `current`/manifest layouts: eligibility and every skip reason, deterministic
   User-then-System execution, pre-mutation revalidation, continuation and
   fail-fast, safe retry after partial success, lock rejection, noninteractive
   confirmation, dry-run non-mutation, elevation before every mutation, denied
   elevation, and truthful human/TOML summaries.

3. Add a compact Textual test-driver suite for durable manager interaction
   contracts: keyboard reachability and scrolling, scope/status filtering while
   keeping focus, duplicated selectors with visible scopes, details and forced
   scope handoff, refresh responsiveness and status changes, non-mutating
   planning, confirmed execution results, cancellation boundary behavior, and
   inventory refresh after an operation. Avoid screenshots, exact spacing, and
   private widget/event-call assertions.

4. Diagnose and fix only defects demonstrated by the new behavior tests. Keep
   manager operations on the existing single-package implementation and do not
   add test-only seams, alternate batch engines, or fake filesystem models.

5. Run the documented Windows manual smoke scenarios that cannot be made
   reliable in CI: User-only/System-only roots, mixed-scope elevation accepted
   and declined, a continuing partial failure and rerun, bootstrap promotion,
   narrow/short terminal behavior, and both human/TOML console output. Record
   reproducible steps in `tests/manual_smoke.md` when needed.

## Completion criteria

- Every issue 011 acceptance criterion is covered by a meaningful automated
  test or explicitly identified as a Windows-only manual smoke check.
- Integration tests use real manager/package directory layouts; mocks stop at
  real external boundaries.
- The manager TUI has durable interaction coverage through Textual's test
  driver.
- Any defect found is fixed with a behavior-oriented regression test.
- `python -m pytest -q` passes from the project virtual environment.
- `git diff --check` passes.
