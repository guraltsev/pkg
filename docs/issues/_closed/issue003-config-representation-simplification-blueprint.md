# Blueprint: simplify config/orchestration representations without splitting `pkg.py`

Date: 2026-04-25
Priority: Medium
Prerequisite: land the metadata consistency fix first

## Goal

Reduce unnecessary internal representations and compatibility scaffolding so the code better matches the project style:

- one main Python file
- simple code
- no generic abstraction
- no future-proofing for hypothetical integrations

This blueprint is a cleanup plan, not an emergency fix. The point is to remove complexity that already caused one real bug.

## Problem statement

The repo is already in one file, but that alone does not make the implementation simple.

The main structural issue is **representation drift**:

- raw config dict
- canonicalized raw config dict
- normalized `PackageConfig`
- compatibility dict reconstructed from `PackageConfig`
- duplicated compatibility fields on `PackageMetadata`

The metadata-consistency bug happened because the code validated the wrong representation.

The large-file issue is secondary. The real problem is that the code carries more layers than the tool needs.

## Evidence

### File and class size

`pkg.py` currently has:

- 4312 lines
- 31 classes
- 60 top-level functions

Large classes include:

- `WindowsPlatform`: `pkg.py:2526-2984` (459 lines)
- `PackageMetadata`: `pkg.py:2985-3832` (848 lines)
- `PackageManager`: `pkg.py:3833-4131` (299 lines)
- `PATHManager`: `pkg.py:2121-2311` (191 lines)

The file being large is acceptable for this project. The issue is that some of that size comes from compatibility wrappers and extra indirection that no longer pull their weight.

### Dead or compatibility-only methods in `PackageMetadata`

The following methods exist but have no internal callers in the repo:

- `_fill_from_directory()` — `pkg.py:3023-3031`
- `_fill_current()` — `pkg.py:3033-3041`
- `_validate_config_dict()` — `pkg.py:3224-3307`
- `_metadata_sync_payload()` — `pkg.py:3322-3330`
- `_create_starter_config_text()` — `pkg.py:3332-3345`
- `_locate_metadata_container()` — `pkg.py:3347-3357`

These are classic examples of “preserve old surface just in case” code. That conflicts with the stated preference against future-proofing.

### `InstallContext` is broader than the install steps need

`InstallContext` carries:

- `identity`
- `config`
- `scope_paths`
- `reporter`
- `force`

Code: `pkg.py:418-435`

But the current step functions mostly use only `context.reporter`, while other data is already available through `metadata`.

Code: `pkg.py:2431-2514`

### `WindowsPlatform` is largely a delegate facade

`WindowsPlatform` mostly forwards to functions or manager classes:

- `resolve_input_path()` -> `resolve_input_path()`
- `compute_scope_paths()` -> `compute_scope_paths()`
- `is_admin()` -> `is_current_user_admin()`
- `update_current_junction_if_needed()` -> `JunctionManager...`
- `install_steps()` -> `INSTALL_STEPS`

Code: `pkg.py:2526-2605`

A small boundary is still useful for tests. The Windows section must be conceptually isolated, but the current shape is more general than the project needs.

## Simplification principles

1. **Preserve one-file organization.**
   - Do not split into modules.

2. **Prefer two config representations, not five.**
   - raw canonicalized dict for file-authored validation / round-trip update work
   - normalized `PackageConfig` for runtime install work

3. **Remove unused compatibility code rather than wrapping it again.**

4. **Keep Windows boundary concrete, not generic.**
   - It is acceptable to keep a small test seam.
   - It is not desirable to keep a broad facade just because a facade is a common pattern.

5. **Do cleanup in slices.**
   - No giant “monolith refactor” PR.

## Implementation plan

### Slice 1: reduce config representations

After the metadata consistency fix lands:

- keep `read_runtime_config()` as the one place that produces both runtime and raw views
- stop reconstructing compatibility dicts from `PackageConfig` for install validation
- keep `PackageMetadata.runtime_config`
- return raw canonicalized config where raw metadata matters

This is the most important simplification because it removes the exact representation drift that caused a bug.

### Slice 2: delete dead compatibility methods in `PackageMetadata`

Remove methods that are not used internally and no longer serve the current architecture:

- `_fill_from_directory()`
- `_fill_current()`
- `_validate_config_dict()` if all validation is already handled by the canonical load path
- `_metadata_sync_payload()` if `metadata_sync_payload()` remains the direct helper
- `_create_starter_config_text()` if `create_starter_config()` is the actual implementation
- `_locate_metadata_container()` if `locate_metadata_container()` is already the direct helper

Before deleting each method, verify no external wrapper scripts or tests depend on it.

### Slice 3: narrow `InstallContext`

There are two good options. Choose the smaller one after checking test impact.

Option A:

- keep `InstallContext`
- reduce it to only what steps actually consume today, likely `reporter` and maybe `scope_paths`

Option B:

- remove `InstallContext`
- pass `reporter` explicitly to each step
- use `metadata` as the source for package/config/scope data

Prefer the option that removes the most indirection without making step signatures noisy.

### Slice 4: shrink `WindowsPlatform` to a minimal boundary

Do **not** remove the idea of a Windows boundary: it still helps tests and keeps the section structure readable.

Do reduce the facade to the smallest surface the package manager truly needs.

Concretely:

- keep only methods that are actually injected or overridden in tests
- remove pass-through attributes if direct references are simpler
- avoid introducing an interface/protocol/adapter hierarchy

### Slice 5: move obviously local imports and constants next to the code that uses them when helpful

The project explicitly allows local imports and constants when that improves readability in a single-file script.

Use that permission only where it makes a section more self-contained. Do not shuffle imports mechanically.

## What not to touch

Do not use this cleanup plan to rewrite these stable areas unless a specific bug demands it:

- variable expansion (`pkg.py:910-994`)
- atomic file writes (`pkg.py:786-847`)
- PATH deduplication (`pkg.py:2121-2309`)
- wrapper file content generation
- shortcut registry/COM primitives in the Windows section

These are not the source of the current large issues.

## Validation plan

1. Keep the existing behavior tests green before each cleanup slice.
2. Add/keep focused tests around metadata consistency so simplification cannot reintroduce the bug.
3. Add a lightweight grep-based quality test only if the team wants to prevent specific legacy wrappers from returning.
4. Review public helper usage before deleting compatibility methods.

## Success criteria

- install path uses only the raw canonical dict and `PackageConfig`, not a third compatibility dict
- dead compatibility methods are removed instead of preserved
- install steps are easier to read because data flow is more direct
- the file remains one sectioned `pkg.py`
- no new abstraction layer is added

## Non-goals

- no split-module architecture
- no dependency injection framework
- no plugin system
- no attempt to generalize for non-Windows platforms
- no redesign of user-facing `pkg.toml`

