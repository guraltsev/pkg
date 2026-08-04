# Blueprint: document reinstall semantics and `current` behavior

Date: 2026-04-25
Priority: High
Change type: Documentation only

## Goal

Document the intended install policy clearly:

- same-version installs are **not** a no-op
- rerunning `Install` is the supported repair path for broken shortcuts, env vars, PATH entries, and wrapper files
- `current` may be recreated as part of that process
- this behavior is intentional and should not be “optimized away” unless the project explicitly changes policy

This blueprint is documentation-only. It does **not** propose code changes.

## Why documentation is needed

A reviewer can easily misread the current code and conclude that same-version installs should be skipped.

That would be the wrong policy for this project.

This repo uses reinstall as a state-reconciliation mechanism. If PATH, shortcuts, wrappers, or env vars are broken, rerunning `Install` should restore them.

The docs need to say that explicitly so future cleanup work does not accidentally remove the behavior.

## Evidence

### Package-variable expansion is anchored to `current`

`_package_variable_map()` expands:

- `$App`
- `$Icons`
- `$Shortcuts`

relative to `<package_root>/current/...`

Code: `gupkg.py:892-907`

That is a strong signal that install is meant to reassert the active package state through `current`.

### Path resolution tracks whether the input version is already current

When the user installs from a version directory, `resolve_input_path()` records:

- `version_is_current = _current_version_matches(package_root, candidate)`

Code: `gupkg.py:1682-1693`

That value is later surfaced through package identity.

### Install intentionally continues for same-version reinstalls

`PackageManager.install()` skips component installation only when:

- the junction did not change
- **and** the target is **not** already current

Code: `gupkg.py:4020-4025`

So when the target version is already current, install continues through the component pipeline even if junction management reports no change.

That matches the desired repair behavior.

### Junction update may recreate `current` even when it already points to the same version

`JunctionManager.update_current_junction_if_needed()` skips only when the existing `current` points to a newer version and `--force` is not set.

Code: `gupkg.py:1814-1817`

For same-version targets, it still goes through temporary junction creation and replacement.

Code: `gupkg.py:1821-1845`

Per current project direction, that is **not worth changing**, but it **must be documented**.

## Documentation targets

### 1. `README.md`

Update the `Actions` section (`README.md:50-57`) to say plainly that:

- `Install` reapplies install state even for the same version
- this is intentional so rerunning install repairs drift in shortcuts, env vars, PATH, wrappers, and the `current` junction

Suggested wording:

> `Install` is repair-oriented: rerunning it for the same version is intentional and reapplies package state such as the `current` junction, shortcuts, environment variables, PATH entries, and wrapper files.

### 2. `docs/architecture.md`

Update the install flow section (`docs/architecture.md:74-86`) to make two points explicit:

- step 5: `current` may be recreated even when it already points at the selected version
- step 6: install steps still run for same-version reinstalls so the system state is repaired, not skipped

Suggested wording for step 5/6 notes:

> The junction step is not only for upgrades. Reinstalling the currently active version may still recreate `current`.
>
> The install pipeline is repair-oriented and still runs for same-version reinstalls so broken external state can be restored.

### 3. `docs/configuration.md`

Update the package-variable section (`docs/configuration.md:67-78`) to connect expansion behavior to install semantics.

Suggested wording:

> Package variables expand through `<package_root>/current/...`. This is intentional: rerunning `Install`, even for the same version, reasserts the active package state that shortcuts, PATH entries, and wrappers resolve through.

### 4. In-code docstrings

Update the docstrings around:

- `JunctionManager.update_current_junction_if_needed()`
- `PackageManager.install()`
- possibly `_package_variable_map()`

so future readers see the same policy in code as in docs.

## Optional documentation test

The repo already has documentation quality tests in `tests/test_quality.py`.

A light-weight addition is reasonable if the team wants the policy guarded:

- assert README contains a phrase like `same version` or `repair-oriented`
- assert architecture docs mention reinstall behavior for the active version

Do this only if the team wants to lock wording intent into tests. Otherwise keep it as plain documentation.

## Success criteria

- README states that same-version install is intentional and repair-oriented
- architecture docs explain that reinstall still runs the install pipeline
- configuration docs explain why package variables point at `current`
- no code changes are made in service of this documentation update

## Non-goals

- no attempt to make same-version installs a no-op
- no attempt to minimize junction churn
- no behavior change to install steps
- no new configuration flags for reinstall policy

