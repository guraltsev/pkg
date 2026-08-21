# Job 02: Manager mode selection and read-oriented CLI

Status: Closed — implemented by commit `19fda65`.

Depends on: Job 01

Design authority: `docs/issues/issue011-fixed-manager-mode-and-bulk-upgrades.md`

## Objective

Expose the manager foundation through deterministic mode resolution and useful
non-installing CLI commands. At the end of this job, users can enter manager mode
from a configured directory or `--config`, list and filter targets, validate
the manager with `doctor`, inspect update availability, and select one package
without weakening existing package-local behavior.

Do not build the manager TUI or `upgrade all` in this job.

## Required preparation

Read the repository instructions and the complete issue 011 design. Review the
Job 01 implementation and current dispatcher in `src/gupkg/gupkg.py`, including
the existing package parser and ad-hoc collection commands.

## Work

1. Add exact manager config discovery:

   - explicit `--config PATH` first;
   - otherwise only `<cwd>/gupkg-config.toml`;
   - never search parents;
   - an invalid marker is an exit-code-2 error rather than a fallback.

2. Preserve the design's resolution priority. An explicit positional package
   path must retain single-package behavior even when the current directory has
   an implicit manager file. An explicitly supplied `--config` must not be
   combined with an unrelated package path.
3. Add manager-aware target and scope selection without duplicating the full
   package parser. Validate contextual scope values only after the invocation
   mode is known.
4. Implement manager forms of:

   ```text
   gupkg list --scope user|system|all
              --filter all|installed|uninstalled|updatable|unhealthy
   gupkg upgrade check --scope user|system|all
   gupkg doctor --scope user|system|all
   gupkg --package SELECTOR --scope user|system [package command...]
   ```

5. `list` should use local state, except that `--filter updatable` must perform
   fresh update checks. `doctor` must validate config, roots, discovery,
   activation, and manifests without contacting update providers.
6. Route selected package commands through existing single-package operations
   with the target's explicit User or Machine scope. Never use Auto for a
   manager-owned target.
7. Implement stable human output and the issue 011 aggregate TOML document.
   `--toml` stdout must remain parseable and free of progress/footer text.
8. Preserve existing exit-code severity and attempt all concerned targets for
   aggregate checks.

The current `_collection_list` accepts filters without implementing them. Do
not preserve that placeholder behavior in manager mode; filters must reflect
the target records or fresh check results they claim to show.

## Observable behavior to protect

Add permanent CLI tests for:

- implicit and explicit manager selection;
- no implicit parent search;
- invalid marker failure without fallback;
- explicit package-path precedence for an implicit marker;
- package-local and ad-hoc collection compatibility;
- all list filters and deterministic summaries;
- complete Doctor diagnostics without network or mutation;
- duplicate-selector errors and scoped resolution;
- user targets forcing User scope even in an elevated process;
- system targets forcing Machine scope;
- parseable TOML output with no interleaved logs; and
- aggregate exit status using the most severe result.

Test through the public `main(argv)` boundary where possible. Mock update
providers, registry/admin state, and other genuine boundaries rather than
private manager helpers.

## Completion criteria

- All required manager read-only commands work from both implicit and explicit
  configuration.
- `gupkg` package-local behavior remains compatible.
- Bare manager invocation fails with a clear temporary message directing the
  user to `list` until Job 03 provides the TUI; it must not enter the ad-hoc
  collection UI or mutate a package.
- Focused CLI tests and the full test suite pass.
- CLI help and examples accurately describe the commands implemented so far.
- `git diff --check` reports no whitespace errors.
