# Part 2 - superfluous code-quality tests that resist refactoring

## Scope

I searched the repo for tests whose main purpose is to enforce documentation wording, source layout, or architectural shape instead of user-visible behavior.

Result:

- the refactor-hostile quality tests are concentrated in **`tests/test_quality.py`**
- `tests/test_pkg_pure.py` is behavior-focused and should stay, aside from updates needed when compatibility surfaces are removed

`tests/test_quality.py` currently contains **16 tests**. Of those, **13 are superfluous refactor blockers** and should be removed. Three are either behavior checks or direct repo-policy checks that still match the stated project goals.

---

## Tests to remove

### A. Remove the entire `DocumentationCoverageTests` class

Definition range:

- `tests/test_quality.py:57-136`

Tests to delete:

- `test_every_function_and_class_has_a_docstring` - `tests/test_quality.py:75-81`
- `test_every_function_docstring_mentions_its_parameters` - `tests/test_quality.py:83-110`
- `test_readme_exposes_documentation_index_and_core_docs` - `tests/test_quality.py:113-120`
- `test_readme_describes_single_file_architecture` - `tests/test_quality.py:122-127`
- `test_docs_index_mentions_all_discoverable_docs` - `tests/test_quality.py:129-136`

### Why these are superfluous and refactor-hostile

They do not protect package-manager behavior. They freeze:

- blanket docstring presence
- docstring wording and parameter mention style
- exact README and docs-index link content
- exact architecture wording in documentation

These tests actively resist the stated goals:

- they bloat the single-file script with mandatory docstrings everywhere
- they make harmless simplifications fail the suite for non-behavior reasons
- they encourage documentation duplication because the test suite checks for redundancy

The docstring tests are especially harmful because they turn code comments into a gating requirement across the whole codebase, including helper scripts.

### Associated dead test support code to delete when this class is removed

- `DefinitionDocVisitor` - `tests/test_quality.py:30-54`
- `DOC_TARGETS` - `tests/test_quality.py:11-14`
- `README` - `tests/test_quality.py:15`
- `DOC_INDEX` - `tests/test_quality.py:16`
- imports used only by those tests: if you keep only the three non-blocking tests, both `ast` and `re` can go too

---

## B. Remove refactor-hostile architecture/source-layout tests

These are all in `ArchitectureBoundaryTests`.

### Remove these methods

- `test_pkg_metadata_does_not_restore_removed_private_compatibility_methods` - `tests/test_quality.py:178-198`
- `test_install_context_wrapper_class_does_not_return` - `tests/test_quality.py:199-204`
- `test_windows_platform_stays_small` - `tests/test_quality.py:205-230`
- `test_shared_windows_and_core_sections_begin_with_imports` - `tests/test_quality.py:232-251`
- `test_windows_markers_are_confined_to_windows_section` - `tests/test_quality.py:253-270`
- `test_core_section_contains_no_direct_windows_mutation_markers` - `tests/test_quality.py:272-287`
- `test_windows_section_keeps_only_wrapper_functions_for_shortcuts_and_registry` - `tests/test_quality.py:289-302`
- `test_orchestration_classes_live_in_core_section` - `tests/test_quality.py:304-317`

### Why these are superfluous and refactor-hostile

They do not test user-visible behavior. They freeze the implementation shape itself:

- exact method names that must not exist
- exact class names that must not return
- exact size/surface assumptions for `WindowsPlatform`
- exact section-level import placement
- exact string markers that may only appear in one section
- exact class/function placement by section

These tests directly obstruct the simplification work from Part 1.

Examples:

- `test_shared_windows_and_core_sections_begin_with_imports` (`tests/test_quality.py:232-251`) directly conflicts with the project preference to keep imports near the code that uses them.
- `test_windows_platform_stays_small` (`tests/test_quality.py:205-230`) blocks removing or reshaping `WindowsPlatform` because it encodes one specific refactor snapshot as policy.
- `test_pkg_metadata_does_not_restore_removed_private_compatibility_methods` (`tests/test_quality.py:178-198`) is an inverse-name blacklist. It does not validate correctness; it just freezes one previous cleanup decision.
- the Windows-marker placement tests (`tests/test_quality.py:253-317`) are brittle string searches over source text, not behavior checks.

These are classic "test the code layout, not the behavior" tests.

---

## Tests that should stay

These are the three tests in `tests/test_quality.py` that are **not** the refactor blockers.

### Keep: wrapper behavior

- `test_wrapper_scripts_preserve_caller_working_directory` - `tests/test_quality.py:140-144`

This is not a style test. It checks a real user-facing property of the wrapper scripts.

### Keep: single-file repo policy (optional but reasonable)

- `test_single_file_layout_removes_old_implementation_modules` - `tests/test_quality.py:167-170`

This aligns with the stated goal of one main Python file.

### Keep: section markers exist (optional but reasonable)

- `test_pkg_py_contains_required_section_markers` - `tests/test_quality.py:172-176`

This aligns with the stated goal that the single file stay organized in sections.

### Note on the two "keep" policy tests

If you want **zero source-structure tests**, you may also delete these two and remove `tests/test_quality.py` entirely. But they are not the tests blocking the refactor.

---

## Removal plan

### Option A - minimal, recommended

Trim `tests/test_quality.py` down to the three useful tests above.

Steps:

1. Delete `DocumentationCoverageTests` entirely.
2. Delete the eight refactor-hostile `ArchitectureBoundaryTests` methods listed above.
3. Delete helper code and constants that only served the removed tests. In the minimal keep-set, `DefinitionDocVisitor`, `_module_tree()`, `_section_ranges()`, and most AST/string-scanning helpers disappear.
4. Keep:
   - wrapper working-directory test
   - single-file-layout test
   - section-marker-exists test

Result:

- `tests/test_quality.py` drops from 16 tests to 3
- the suite stops policing docstrings, wording, imports-at-top, and exact class placement
- the remaining checks still match the stated project goals

### Option B - full removal

If you want the suite to be behavior-only, delete `tests/test_quality.py` entirely and move the wrapper working-directory check into a small wrapper-script test file.

This is valid if you no longer want any repo-shape tests at all.

---

## Why these removals should happen before the code refactor

If you keep the 13 superfluous tests while simplifying the code, they will generate noise and false negatives for the exact changes you want:

- removing compatibility facades
- moving imports closer to use
- deleting documentation bloat
- deleting old wrapper/adapter surfaces
- simplifying section internals without preserving exact string markers

The right order is:

1. remove the refactor-hostile quality tests
2. simplify the code
3. keep or add behavior tests only where user-visible behavior needs protection

---

## What to keep testing after these removals

The behavior suite in `tests/test_pkg_pure.py` should remain the main guardrail.

After Part 1 lands, update behavior tests so they validate:

- canonical config parsing only
- metadata sync on canonical top-level keys only
- install behavior from typed runtime config
- wrapper script behavior
- shortcut, env, PATH, and config-update behavior

Do **not** replace the removed quality tests with lint-style source-shape tests unless you explicitly want the suite to keep resisting refactoring.
