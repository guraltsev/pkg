# Agent Instructions

## Tests

`tests/user` is reserved for high-level behavior. Keep tests focused on what notebook users can do or see.

Do not add low-value tests derived from the implementation process to `tests/user`.

You may add low-value tests derived from the implementation process to `tests/devel` as scaffolding for refactoring. This scaffolding should not be run outside of refactoring sessions. 

Do not add tests that lock down private helpers, module structure, file layout, generated-code details, documentation notebook templates, or temporary refactor choices.

Add or keep a test only when it protects a meaningful user-facing regression.

## Design decisions 

Do not use tests for design constraints and decisions.

Do use documentation for significant design constraints that are not observable behavior.

Do not document simple design choices that can be directly observed in code. 
