## General guidelines

`tests` must contain high-value high quality tests that check functionality.

`tests` must NOT contain low-quality churn tests that lock in specifics of implementation and are used temporarily for refactoring. 


`tests/devel` can contain temporary tests used during large refactors BUT they must be clearly marked as temporary and should not be run outside refactoring sessions.
The default pytest configuration excludes `tests/devel`; run those tests by explicit path only during the refactor that needs them.

Do not add low-value tests that lock down code structure. A test belongs here only when a failure would describe a meaningful regression in functionality.

 
Do use documentation in module docstrings or in `/docs/` to explain architectural and design decisions that are not immediate from a quick glance at the code. 

Do not use tests for design decisions or other constraints that are not observable behavior.
 
Do not add tests here for implementation-process artifacts:

- private helpers or private module functions
- module boundaries, file layout, or internal data structures
- exact generated JavaScript or HTML internals beyond user-visible links/text
- documentation notebook cell order or other authoring templates
- assertions that exist only because of how a change was implemented

## Project specific guidelines

Tests may patch OS mutation boundaries to avoid touching the real machine, but assertions should stay on CLI-visible behavior, user-authored files, generated user-facing artifacts, or documented helper workflows.