# Documentation index

## For people using gupkg

Start at the top and stop when you have what you need.

1. [getting_started.md](getting_started.md) — install your first program and
   update it in five minutes.
2. [cookbook.md](cookbook.md) — recipes: nightly updates, maintenance windows,
   rollback, CI checks, adopting existing programs, repairs.
3. [troubleshooting.md](troubleshooting.md) — every error message with its fix.
4. [operations.md](operations.md) — the operator runbook: standalone
   installation, manager configuration, registry sync/search/install, offline
   operation, self-repair, migration, recovery, and release validation.
5. [../README.md](../README.md) — the complete `pkg.toml` and command reference.

`gupkg --help` and `gupkg <command> --help` are always the authoritative list of
options for the version you have installed.

## For people changing gupkg

- [development_guide.md](development_guide.md) — implementation boundaries and
  update architecture.
- [python_rules.md](python_rules.md) — Python implementation style, including
  code organization and block comments.
- [docstring_schema.md](docstring_schema.md) — docstring style.
- [tests.md](tests.md) — testing policy and what belongs in the suite.
- [tui_style_guide.md](tui_style_guide.md) — principles, interaction model, and
  visual style for the list-first terminal interfaces.
- [../tests/manual_smoke.md](../tests/manual_smoke.md) — Windows, elevation, and
  release smoke checks.
- [issues/](issues/) — design records and acceptance criteria for past and
  open work.

`AGENTS.md` is only a pointer to the style and testing files. Put human-facing
rationale and style rules here.
