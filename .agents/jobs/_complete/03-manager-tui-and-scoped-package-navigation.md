# Job 03: Manager TUI and scoped package navigation

Status: Closed — implemented by commit `3e4d7ba`.

Depends on: Job 02

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Add the interactive manager home, scrollable package browser, status refresh,
details, and entry into existing package operations. At the end of this job,
bare `gupkg` in a manager directory provides a useful list-first TUI for both
configured roots.

Do not implement bulk mutation in this job. The Upgrade All row may be absent
or visibly disabled with a concise “not implemented yet” label until Job 05.

## Required preparation

Read the repository instructions, issue 011, and
`docs/tui_style_guide.md`. Inspect `tui.py`, `collection_tui.py`, and the
manager CLI/domain delivered by Jobs 01 and 02.

## Work

1. Add `src/gupkg/manager_tui.py` as a presentation adapter over the manager
   domain. Import Textual only at the presentation edge.
2. Implement the issue 011 manager home with manager-file context, root/package
   counts, concise incomplete-root warnings, and actions for browsing,
   refreshing update status, Doctor, and version information.
3. Implement a naturally scrollable, borderless package `OptionList`. Rows must
   visibly include selector, scope, installed/broken/not-installed status,
   installed version when present, and update state without relying on color.
4. Implement in-place scope and status filters while retaining focus. Long
   selectors and descriptions should have a plain detail view rather than
   distorting the list.
5. Run update checks outside Textual's event loop. Open a result/progress view
   before work starts and refresh target rows from completed results.
6. Selecting a target should open details and then the existing package
   operation UI. Extend that UI with an optional forced scope or equivalent
   context:

   - user-root targets are locked to User;
   - system-root targets are locked to Machine;
   - ordinary package-local calls keep current automatic scope behavior;
   - the locked scope is visible, not silently applied.

7. Refresh local manager inventory when the user returns from a package
   operation so installed versions and health do not remain stale.
8. Replace Job 02's temporary bare-invocation message with manager TUI routing.

Follow the TUI guide: no decorative frames, Header/Footer chrome, button bars,
or parallel panes. Up/Down and Enter must reach ordinary choices; Escape goes
back or exits at home; `q` exits consistently.

## Observable behavior to protect

Use Textual's test driver for a small set of durable contracts:

- bare manager invocation enters the manager app;
- package rows scroll and every target is keyboard reachable;
- scope and status filters visibly change the rows and preserve focus;
- duplicate selectors show separate User and System rows;
- missing roots remain visible through warning/Doctor behavior;
- selecting a target reaches details and package operations;
- forced scope is visible and cannot drift;
- update refresh leaves the app responsive and updates row state; and
- returning from package operations refreshes installed state.

Do not add screenshot snapshots, exact-spacing assertions, widget-tree shape
tests, or tests of private event-handler call order.

## Completion criteria

- Bare manager invocation opens a responsive, list-first TUI.
- The same manager domain state drives CLI and TUI labels.
- No bulk mutation is reachable yet.
- Package-local TUI behavior remains compatible.
- Focused TUI tests and the full test suite pass.
- Manually smoke-test a terminal shorter than the package list and confirm that
  selection remains visible while scrolling.
- `git diff --check` reports no whitespace errors.
