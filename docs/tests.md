# Test Policy

`tests/` contains the project's test suites. Permanent unit tests currently
live under `tests/unit/`, and broader suites such as `tests/integration/` or
`tests/e2e/` can live alongside them as needed.

A test belongs only if its failure would describe a real functional regression. 

Do not add tests that preserve implementation details, refactoring choices, file layout, or documentation artifacts.

Before adding or changing a test, state the behavior it protects. If the behavior is not observable but important, document it instead.

## What to Test

Add permanent tests for:

* public API behavior
* user-visible output, links, text, errors, and side effects
* meaningful integration across real project boundaries
* regressions users or downstream callers could notice

Do not add permanent tests for:

* private helpers or private module functions
* internal call graphs
* module boundaries, file layout, or internal data structures
* exact generated JavaScript or HTML internals beyond user-visible links or text
* documentation notebook cell order or authoring templates
* assertions that exist only because of how a change was implemented
* design constraints that are not observable behavior

Use documentation instead of tests for non-observable design constraints:

* module docstrings: public purpose and behavior
* `docs/`: long-lived architecture and design guidance
* code comments: local implementation rationale

## Layout

Organize permanent tests by feature area.

* `tests/fixtures/`: fixture projects
* `tests/*/` permanent unit tests grouped by module or functionality area
* `tests/integration/`, `tests/e2e/`: optional broader suites when cross-feature
  or end-to-end coverage becomes necessary
* `tests/_devel/`: temporary refactor tests

The default pytest configuration excludes `tests/_devel/`. Run those tests only by explicit path during the refactor that needs them. Mark them clearly as temporary and remove them when no longer needed.

## Test Modules

Every permanent test module starts with a docstring stating:

* behavior covered
* boundaries mocked vs real
* what is out of scope

## Naming and Grouping

Test names should read like behavior statements.

Each test function must have a docstring that briefly states the behavior being tested. The docstring should describe the observable contract, not the implementation path. The docstring must start with a one-sentence summary paragraph. It is followed by an optional paragraph providing more details.

Prefer plain test functions. Use classes only when they make a behavior family easier to read.


## Fixtures and Helpers

When using fixtures, document:

* what the fixture represents
* where expected output lives
* which behavior the fixture verifies

Helpers should make tests clearer, not hide the behavior.

## Parameterization

Parameterize only when it improves readability.

Every parameter set must have a readable `id=`.

## Mocking

Mock real boundaries only, such as network, subprocesses, clocks, external services, or nondeterministic dependencies.

Do not mock private helpers, internal call graphs, or implementation steps. A test should remain valid when the implementation changes but observable behavior stays the same.

## LLM-Aided Development

When using an LLM to write or revise tests:

* start from the behavior contract, not the current implementation
* do not invent expected behavior
* do not add tests only to increase coverage
* do not preserve accidental behavior unless it is intentionally user-visible
* prefer fewer clear tests over many narrow tests that lock down code shape


## Development tests

`tests/` is the default pytest target. Keep stable unit coverage under
`tests/`. Add `tests/integration/` or `tests/e2e/` only for broader workflows
that should be part of default discovery.
`tests/_devel/` can contain temporary refactor scaffolding. It is excluded from
default pytest discovery and should be run only by explicit path during the
refactor that needs it. `tests/_devel/` is available for procedural
deep-analysis tests that report useful facts without becoming CI or regression
tests. Use dedicated subdirectories and put a local `README.md` in that
subdirectory, run those tests explicitly with `-s`, and keep their assertions
limited to light sanity checks that let the report complete.


# Project-Specific Guidelines
