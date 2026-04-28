# Blueprint: simplify `pkg.py` by removing internal representation layers

Date: 2026-04-28
Priority: High
Change type: Targeted structural simplification

## Purpose

This blueprint is for a focused simplification pass on the repository.

It is not a rewrite, not a feature expansion, and not a re-architecture into more modules.
The repo already has the right outer shape in the ways that matter:

- one main implementation file: `pkg.py`
- explicit top-level action dispatch in `main()`
- one canonical config file shape: `pkg.toml`
- behavior-oriented tests

The problem is narrower and more specific:

`pkg.py` still carries too much **internal representation machinery** for a tool whose runtime flow is fixed and whose configuration format is already stable.

The goal of this change is to make the codebase better match the intended project style:

- one Python file for main functionality
- simple configuration
- simple code
- no generic abstraction
- no speculative future-proofing
- minimal OO, only where it clearly improves correctness or readability
- detailed, useful documentation on coordinating functions rather than on passive schema wrappers

---

## Problem statement

The main issue is not merely that there are “too many dataclasses.”

The core issue is that `pkg.py` still preserves an **internal model layer** that does not buy enough correctness, encapsulation, or flexibility to justify its complexity.

This shows up as:

- dataclasses that mostly act as passive field bags
- objects with fields that are never read by runtime behavior
- repeated conversion between one short-lived representation and another
- validation that still happens outside the objects, meaning the objects are not enforcing the invariants they imply
- pass-through wrapper functions whose only job is to translate one local representation into another
- small framework-like constructs for a fixed install sequence that does not need a registry model
- contributor documentation that describes these internal layers as if they are part of the intended architecture

This is why the repo feels more abstract than necessary even though the implementation is already in a single file.

---

## Design decision

The correct fix is **targeted flattening**.

That means:

- keep the single-file shape
- keep the current user-visible behavior
- keep only the few types that clearly represent durable concepts or action results
- remove or inline the rest of the short-lived internal schema objects
- replace generic pipeline machinery with explicit code where the order is fixed
- keep documentation, but move it toward the functions that actually coordinate behavior

This blueprint intentionally avoids unrelated cleanup. It is not trying to touch stable Windows helper logic, file-write safety code, version comparison logic, junction handling, or other parts that are not causing the abstraction problem.

---

## Evidence summary

This blueprint is based on direct repo inspection.

### 1. Large passive dataclass block at the start of `pkg.py`

The beginning of `pkg.py` contains a large run of enums/dataclasses, including:

- `StepResult`
- `ActionResult`
- `ResolvedInput`
- `PackageIdentity`
- `ScopePaths`
- `ShortcutSpec`
- `EnvVarSpec`
- `BinSpec`
- `PackageConfig`
- `ExpansionResult`
- `PreparedShortcut`

Most of these are passive data containers.

### 2. `ScopePaths` carries dead state

`ScopePaths` stores:

- `scope`
- `shortcut_root`
- `bin_dir`
- `env_root`
- `env_subkey`

But runtime behavior only uses the path-like members relevant to the filesystem install flow, while some stored fields are not read later.

This indicates the object is broader than the real needs of the runtime.

### 3. `ResolvedInput` carries dead state

`ResolvedInput` stores more input-description fields than downstream runtime behavior actually uses.
This means the object is documenting input shape more than helping the runtime.

### 4. Config row dataclasses do not enforce their own invariants

`ShortcutSpec`, `EnvVarSpec`, `BinSpec`, and `PackageConfig` still require external validation later.
That shows they are not functioning as strong domain objects. They are mainly a second representation of config data.

### 5. Shortcut flow has multiple one-shot layers

Shortcut entries flow through multiple wrappers before becoming actual install operations:

raw config row -> normalized object -> prepared object -> write operation

That is too much structure for a fixed, narrow transformation.

### 6. Fixed install sequence is modeled as a step registry

The current install flow uses a step list / step-call abstraction even though the sequence is fixed and known.
That adds indirection without adding meaningful flexibility.

### 7. Development documentation currently reinforces the extra layers

`docs/development.md` documents internal representations and pipeline structure that should be simplified or removed.
If that documentation is left unchanged, future contributors will naturally rebuild the same complexity.

---

## Non-negotiable constraints

These constraints apply throughout the implementation.

1. Keep **one main implementation file**: `pkg.py`.
2. Do **not** split the logic into more runtime modules.
3. Do **not** add plugin systems, registries, base classes, or generic handler abstractions.
4. Do **not** rewrite behavior that is already working and unrelated to the representation problem.
5. Preserve user-visible behavior unless this blueprint explicitly calls for a documentation-only clarification.
6. Preserve the existing config format and the current `pkg.toml` contract.
7. Preserve and improve docstrings; do not add tests that enforce style or source layout.
8. Keep the code explicit, even when that means slightly repetitive `if` statements or direct function calls.
9. Avoid import hoisting for its own sake. Keeping imports close to the code that uses them is acceptable when it improves readability.
10. Do not try to generalize for hypothetical future package formats or install pipelines.

---

## Desired final architecture

At the end of this change, the repo should follow this shape:

### A. Keep only a small set of justified core types

These are reasonable to keep because they represent durable concepts or final outputs:

- `Scope`
- `Action`
- `StepResult`
- `ActionResult`
- `PackageIdentity`
- possibly `ExpansionResult` if it still materially improves clarity

Everything else should justify itself against a high bar. If it is only a short-lived carrier for config rows or contextual data, it should be removed or inlined.

### B. Operate on canonical dict/list config structures

Instead of parsing config rows into a separate family of dataclasses and then validating/unpacking them later, the runtime should work with a normalized canonical dict/list representation that stays close to the `pkg.toml` shape.

### C. Replace fixed-pipeline registries with explicit sequencing

The install pipeline should be visible as explicit ordered code, not as a small registry framework.

### D. Replace context-wrapper objects with direct local values

When a function needs only a few values, pass those values directly or group them into a small plain mapping local to that flow.
Do not create a dedicated class just because there are three to five related fields.

### E. Move detailed documentation to coordinating functions

Long, detailed docstrings should live on functions that actually coordinate behavior, validate state transitions, or shape runtime data.
Passive helper definitions should not consume most of the documentation attention.

---

## Scope of change

### In scope

- simplify `pkg.py` internal data representations
- reduce or remove unnecessary dataclasses
- remove fixed-flow generic step abstractions
- simplify config normalization / validation flow
- simplify shortcut preparation flow
- improve docstrings and section comments where the coordination logic lives
- update `docs/development.md` so it reflects the simplified architecture
- update tests only where representation removal changes helper boundaries or text that tests indirectly rely on

### Out of scope

Do not redesign or broadly refactor these unless required by the simplification work:

- junction/current handling semantics
- version comparison policy
- wrapper file behavior
- Windows registry mutation helpers
- environment variable persistence behavior
- helper script purpose and behavior unless it directly depends on a removed internal type
- `.gitconfig` support logic
- packaging/distribution strategy
- test philosophy beyond removing or adjusting representation-coupled assumptions if any are encountered

---

## Detailed implementation plan

## Phase 1: Freeze behavior and map the current representation flow

### Goal

Make the simplification safe by identifying exactly where each internal representation begins and ends.

### Tasks

1. Add a temporary working note for the refactor branch that maps these flows:
   - input path resolution
   - scope path resolution
   - runtime config normalization
   - shortcut install preparation
   - install step orchestration

2. For each of these current objects, list:
   - where it is constructed
   - where it is consumed
   - which fields are actually read
   - whether it enforces any invariant that would be lost if removed

Objects to inspect:

- `ResolvedInput`
- `ScopePaths`
- `ShortcutSpec`
- `EnvVarSpec`
- `BinSpec`
- `PackageConfig`
- `PreparedShortcut`
- `PackageMetadata`
- install-step registry helpers

### Exit criteria

- There is a precise local understanding of what each object currently does.
- No refactor begins from guesswork.

---

## Phase 2: Remove dead fields and reduce context objects first

### Goal

Start with the least risky simplifications: eliminate fields and wrappers that are clearly broader than runtime needs.

### Tasks

1. Refactor input-resolution flow so `ResolvedInput` is removed.

   Replace it with one of these two shapes:

   Preferred:
   - return `PackageIdentity`
   - return `installing_from_current` as a separate boolean

   Acceptable alternative:
   - return a plain dict with only the fields the runtime actually uses

   Do not preserve fields like raw input description unless they are genuinely needed downstream.

2. Refactor scope path resolution so `ScopePaths` is removed.

   Replace it with a helper like:

   - `compute_scope_paths(scope) -> dict[str, Path]`

   Keep only the path values actually needed by runtime behavior.

   Important:
   - compute these values at runtime
   - do not precompute them at import time
   - keep environment-variable reading compatible with tests that patch environment variables

3. Remove any now-unused helper methods that only existed to support these deleted objects.

### Why this phase comes early

These changes reduce broad context passing without yet disturbing config parsing or install sequencing.
That makes the next phases smaller and easier to reason about.

### Exit criteria

- `ResolvedInput` is gone.
- `ScopePaths` is gone.
- The surrounding code is clearer and uses only the values the runtime needs.
- Tests still pass.

---

## Phase 3: Collapse config row dataclasses into canonical normalized dict/list config

### Goal

Remove the second internal schema layer for runtime config.

### Current problem

The code currently normalizes config into dataclass objects such as `ShortcutSpec`, `EnvVarSpec`, `BinSpec`, and `PackageConfig`, but later still validates their required fields separately.
That means these objects are not the real source of truth.

### Target state

Keep one normalized runtime config representation that is still dict/list based and close to the canonical `pkg.toml` structure.

Example target shape:

```python
{
    "only_portable": False,
    "shortcut": [
        {
            "name": "Tool",
            "targetPath": "$App\\tool.exe",
            "arguments": "",
            "workingDirectory": "",
            "iconLocation": "",
            "description": "",
        }
    ],
    "environment": [
        {"Name": "TOOL_HOME", "Value": "$App"}
    ],
    "path": ["$App"],
    "bin": [
        {"name": "tool.cmd", "content": "..."}
    ],
    "description": "...",
    "homepage": "...",
    "downloadURL": "...",
}
```

### Tasks

1. Rewrite `normalize_runtime_config()` so it returns canonical normalized dict/list data, not `PackageConfig`.

2. Remove:
   - `ShortcutSpec`
   - `EnvVarSpec`
   - `BinSpec`
   - `PackageConfig`

3. Keep validation logic, but make it validate the normalized dict/list structure directly.

4. Ensure defaults are still filled in explicitly during normalization.

5. Ensure warning/error messages remain as stable as possible.

6. Remove any computation inside normalization that is not used by the returned structure or subsequent validation.

### Important rule

Do not replace the deleted dataclasses with a new abstract generic validation framework.
Use explicit normalization and explicit validation functions.

### Exit criteria

- Runtime config is represented once, not twice.
- All row-level config dataclasses are gone.
- Validation still catches the same invalid states.
- Tests still pass.

---

## Phase 4: Flatten shortcut preparation flow

### Goal

Remove the extra one-shot representation used by shortcut install.

### Current problem

Shortcut entries are transformed multiple times before use, including an intermediate prepared-object layer.
That is too much structure for a direct install operation.

### Tasks

1. Remove `PreparedShortcut`.
2. Remove any pass-through helper whose sole job is to create `PreparedShortcut` from `ShortcutSpec`.
3. Refactor shortcut installation so it either:
   - validates and expands each normalized shortcut dict directly in the install loop, or
   - uses a small helper returning a plain dict/tuple local to shortcut creation

4. Keep the actual PowerShell / file-generation behavior unchanged.

### Preferred style

The transformation from normalized shortcut config row to concrete shortcut creation inputs should be visible in one place.
A reader should not have to jump across several wrappers to understand how a shortcut is created.

### Exit criteria

- `PreparedShortcut` is gone.
- Shortcut handling is explicit and local.
- User-visible shortcut behavior remains unchanged.

---

## Phase 5: Remove `PackageMetadata` and pass explicit values through the install flow

### Goal

Replace the broad context-wrapper object with direct data flow.

### Current problem

`PackageMetadata` groups identity, scope, scope paths, and runtime config, but many consumers only need a subset.
It acts mainly as a convenience wrapper plus a place for guard methods.

### Tasks

1. Remove `PackageMetadata`.
2. Refactor `install_package()` and nearby helpers to pass explicit values such as:
   - `identity`
   - `scope`
   - `scope_paths`
   - `runtime_config`
   - `warnings`

3. Inline or replace guard methods like `require_scope_paths()` and `require_runtime_config()` with clearer local control flow.

4. Where a helper needs only one or two values, pass only those values.

### Important rule

Do not replace `PackageMetadata` with another broad context dataclass under a different name.
The point is to reduce hidden coupling, not rename it.

### Exit criteria

- `PackageMetadata` is gone.
- Install flow uses explicit data passing.
- The call graph is easier to read top-down.

---

## Phase 6: Replace the install-step registry with explicit ordered calls

### Goal

Remove the small pipeline abstraction around a fixed install order.

### Current problem

The install sequence is fixed, known, and not extensible in practice, but it is represented as step-call objects and a registry-like ordered list.
That adds abstraction without value.

### Tasks

1. Remove:
   - `InstallStep` callable alias if it becomes unnecessary
   - step wrapper helpers that only adapt signatures
   - `INSTALL_STEPS`

2. Rewrite the install orchestration as direct code in one place, for example:
   - install shortcuts
   - install environment variables
   - ensure bin directory in PATH if applicable
   - install additional PATH entries
   - install wrapper/bin files

3. Keep result accumulation explicit.
4. Keep the order unchanged unless a correctness reason requires otherwise.

### Preferred style

Use direct local variables and direct function calls.
Slight repetition is acceptable if it removes indirection.

### Exit criteria

- There is no registry-like install-step layer.
- A reader can see the install order immediately in one local code block.
- Result reporting stays correct.

---

## Phase 7: Rebalance documentation toward the real coordinators

### Goal

Keep strong documentation, but place it where it helps maintainers understand behavior.

### Current problem

The code already has broad docstring coverage, but some documentation effort is spent on passive wrappers that should disappear.
The detailed explanation should instead live on functions that coordinate behavior and state transitions.

### Tasks

1. Ensure all remaining functions have docstrings.
   - short docstrings for small local helpers
   - detailed docstrings for coordinating functions

2. Expand the docstrings for the main coordinating functions, such as:
   - input resolution
   - config normalization
   - config validation
   - install orchestration
   - current-junction update logic
   - metadata sync/update-config logic

3. Keep docstrings concrete.
   They should explain:
   - what the function takes
   - what invariants it assumes
   - what transformation it performs
   - why the function exists
   - any subtle policy constraints that future cleanup must preserve

4. Remove docstrings/comments that over-describe deleted wrappers or no longer reflect reality.

5. Update `docs/development.md` so it reflects the simplified design:
   - no mention of removed wrapper objects as architectural pillars
   - no mention of an install-step registry if removed
   - explicitly state that the code should prefer direct functions and direct data flow over internal schema layering

### Documentation principle

The documentation should help future maintainers preserve simplicity.
It should not accidentally invite them to rebuild a mini-framework inside `pkg.py`.

### Exit criteria

- Documentation matches the new architecture.
- There are no stale references to deleted types or flow patterns.

---

## Phase 8: Test adjustment and verification

### Goal

Prove the simplification preserved behavior and did not become a stopgap.

### Tasks

1. Run the full test suite after each major phase.
2. Update tests only where they indirectly relied on removed internal representations.
3. Do not add tests that enforce code layout, class counts, section names, or wording style.
4. Add or adjust behavior tests only if the refactor reveals a previously untested user-visible contract.

### Minimum verification checklist

- existing full test suite passes
- install/update config behavior remains unchanged
- invalid config validation still fails in the same scenarios
- same-version install semantics are preserved
- `current` handling is preserved
- shortcut generation behavior is preserved
- wrapper/bin behavior is preserved
- environment variable and PATH behavior are preserved

---

## Exact removal targets

These are the main removal targets unless implementation reveals a narrow reason to keep one.

### Remove entirely

- `ResolvedInput`
- `ScopePaths`
- `ShortcutSpec`
- `EnvVarSpec`
- `BinSpec`
- `PackageConfig`
- `PreparedShortcut`
- `PackageMetadata`
- install-step registry/list abstraction and its pass-through wrappers

### Keep only if still justified after refactor

- `ExpansionResult`

This may still be worthwhile if it materially improves correctness or readability around expansion results. If it ends up being just another field bag, remove it too.

### Explicitly keep

- `Scope`
- `Action`
- `StepResult`
- `ActionResult`
- `PackageIdentity`

These represent stable concepts or action outcomes and are not the source of the abstraction problem.

---

## What not to do

The implementation must avoid these failure modes.

### 1. Do not rename abstractions instead of removing them

Bad example:
- replace `PackageMetadata` with `InstallContext`
- replace `ScopePaths` with `ScopePathSet`

That is not simplification. That is the same design with new names.

### 2. Do not introduce typed-dict forests, protocol layers, or validator frameworks

The point is not to swap dataclasses for a different abstract schema system.
Keep the code direct.

### 3. Do not move complexity into utility layers or helper modules

The repo should remain centered around one main file and explicit local logic.

### 4. Do not make import placement more ceremonial than useful

If a helper or import is only relevant in one local section, it is acceptable to keep it near that section.

### 5. Do not touch unrelated stable subsystems in the name of cleanliness

This is a targeted simplification, not a broad style sweep.

### 6. Do not add tests that freeze source shape

No tests for:
- class counts
- exact docstring phrasing
- section headers
- source ordering
- “must remain dataclass-free” style rules

The code should be simpler by design, not by brittle test enforcement.

---

## Why this is the right approach

This plan addresses the **cause** of the current complexity, not just a visible example.

A shallow fix would only:

- trim one field from `ScopePaths`
- rename a couple of classes
- shorten a few docstrings
- move code around without changing the number of representations

That would not solve the real problem.

This plan instead removes the extra internal layers that force the reader to constantly translate between:

- raw config shape
- normalized object shape
- prepared object shape
- broad metadata wrapper shape
- registry-driven install flow shape

By collapsing those layers, the code becomes simpler in the exact sense the project is aiming for:

- fewer abstractions
- fewer passive classes
- more obvious control flow
- less representational drift
- documentation focused on behavior rather than scaffolding

It is also appropriately narrow.
It does not rewrite working Windows-specific behavior or otherwise churn stable logic that is not part of the issue.

---

## Recommended implementation order

Use this order to reduce risk:

1. Remove `ResolvedInput`
2. Remove `ScopePaths`
3. Replace runtime config dataclasses with canonical normalized dict/list config
4. Remove `PreparedShortcut` and flatten shortcut preparation
5. Remove `PackageMetadata`
6. Replace install-step registry with explicit ordered calls
7. Update docstrings and `docs/development.md`
8. Final cleanup of any dead helpers/imports/comments

This order moves from low-risk contextual simplification toward broader flow simplification.
It also ensures that later phases are built on already-simplified inputs.

---

## Acceptance criteria

The refactor is complete when all of the following are true:

1. `pkg.py` remains the single main implementation file.
2. The removed wrapper/dataclass layer is gone or materially reduced to only justified core types.
3. No replacement abstraction layer has been introduced under different names.
4. Runtime config is represented in one canonical normalized form close to `pkg.toml`.
5. Install flow is visible as explicit ordered logic.
6. The code passes the full test suite.
7. Documentation reflects the simplified architecture.
8. The resulting code is easier to read top-down without jumping across pass-through wrappers and passive schema classes.

---

## Suggested final review questions

Before merging, explicitly check:

- Did we truly remove the abstraction, or only rename it?
- Does every remaining class represent a durable concept or final result rather than short-lived glue?
- Is any function still converting between two internal shapes that could simply be one shape?
- Can a maintainer now understand install flow by reading one top-down section of `pkg.py`?
- Do the docstrings explain coordination logic rather than defending internal scaffolding?
- Did we avoid touching unrelated parts of the repo?

If the answer to any of these is no, the simplification is incomplete.

---

## Final expected outcome

After this change, the repo should still feel powerful, but it should feel much less architectural.

It should read like a deliberate single-file tool:

- explicit actions
- explicit normalization
- explicit validation
- explicit install sequencing
- a small number of justified core types
- strong documentation on the real behavior

That is the correct target for this codebase.
