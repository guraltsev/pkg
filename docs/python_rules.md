# Python Programming Style

These rules control Python programming style. Use `docs/docstring_schema.md`
for docstrings and public documentation.

Write code as an explained calculation: the reader should understand the role
of each small block before reading every line inside it.

## Reading order

Organize files by narrative importance.

1. Put general imports first.
2. Put local, optional, heavy, platform-specific, or one-off imports near the
   code that first needs them.
3. Put the public entrypoint, command handler, or central workflow before
   low-level implementation details.
4. Put supporting helpers later, grouped by purpose.
5. Use short section comments when a file has distinct groups of helpers.

Do not start with a long catalog of constants, helpers, classes, types, and
configuration unless the reader needs those names immediately.

Module docstrings describe public purpose and observable behavior. They should
not explain where to start reading the source file; use comments for local
source narration.

## Narrated code blocks

A nontrivial function should read as a sequence of short, narrated blocks. Put
a one- or two-line comment before each block that explains what the next few
lines accomplish.

Use block comments before:

- validation or normalization steps,
- loops that collect, transform, filter, copy, or verify data,
- corner-case handling,
- filesystem, subprocess, network, or other side-effecting operations,
- conversions between public inputs and internal representations,
- construction of summaries, manifests, reports, or output records,
- fallback behavior,
- safety checks,
- dense imperative sequences.

A good comment describes the move being made in the function's argument. It
answers questions such as:

- What are we establishing?
- What case are we handling?
- What object are we constructing?
- What invariant or safety property are we preserving?
- What side effect is about to happen?

A reader should rarely encounter more than about ten lines of nontrivial
imperative code without a leading comment. Tiny one-line operations do not need
comments.

Bad:

```python
# Loop over the paths.
for path in paths:
    copy(path)
```

Good:

```python
# Resolve every input before creating outputs, so invalid globs fail without
# leaving a partially populated build directory.
for path in paths:
    copy(path)
```

Bad:

```python
# Check if step_dir is not None.
if step_dir is not None:
    write_summary(step_dir)
```

Good:

```python
# Emit pipeline diagnostics only when the caller requested a step directory.
# The summary reflects the files actually produced by this run.
if step_dir is not None:
    write_summary(step_dir)
```

A comment is bad if it merely repeats the next line. A comment is good if it
lets the reader skip the next few lines.

## Control flow and helpers

Keep simple logic local and readable in one pass. Prefer direct iteration and
immediate error signaling over helper indirection.

Do:

- keep validation and action together when used once,
- iterate directly over values,
- raise immediately with a clear message when a value is invalid,
- use straightforward control flow over extra abstraction,
- split code into helpers when the helper names a real concept, isolates a
  side effect, removes genuine repetition, or makes behavior independently
  testable.

Do not:

- extract one-off validation into tiny private helpers,
- add normalization or conversion layers unless behavior requires them,
- introduce extra state or containers that are not needed for correctness,
- hide the main behavior behind indirection,
- create tiny one-use helpers merely to make the code look organized.

Heuristic: if the reader must jump between functions to follow one simple
operation, inline it.

## Classes and state

Use classes only when there is meaningful persistent state, invariants,
multiple instances, or a natural object interface. Do not use classes as
namespaces for loosely related functions.

Keep state close to the behavior that owns it. Avoid generic registries,
configuration objects, or framework-style architecture unless the behavior
requires them.

## Constants

Constants are useful when they are reused, conceptually important, or likely to
change. Otherwise, keep values close to where they are used.

Keep one-use constants local to their consumer. Put module-level constants at
the top only when they are genuinely module-relevant. Every global constant
must have a short comment explaining its role.

Bad:

```python
USER_FIELDS = ("id", "name", "email")
ORDER_FIELDS = ("id", "total", "created_at")


def export_rows(user, order):
    user_row = {field: getattr(user, field) for field in USER_FIELDS}
    order_row = {field: getattr(order, field) for field in ORDER_FIELDS}
    return user_row, order_row
```

Good:

```python
def export_rows(user, order):
    # Keep each export schema next to the row it constructs.
    user_fields = ("id", "name", "email")
    user_row = {field: getattr(user, field) for field in user_fields}

    # The order row is independent of the user row and has its own schema.
    order_fields = ("id", "total", "created_at")
    order_row = {field: getattr(order, field) for field in order_fields}

    return user_row, order_row
```

## Explanatory prose

When writing examples, reviews, or design notes, introduce the code before
showing it. Do not put the only explanation after the code as a retrospective
tour.

Prefer direct, readable code over generic, reusable, or enterprise-style
architecture.

# Instructions specific to this repo

Each .py file in this repo is self-contained and its logic is NOT split into submodules. 

It stays organized through
clearly labeled sections that correspond to logical boundaries and roughly correspond to submodules. 

Section formatting:

```python
#------------------------------------------
# Section: Shared models and pure helpers
#------------------------------------------
#
# [... Short documentation of section]
```

The .py file docstring  at the top MUST contain a section TOC/guide:


```python

Section guide
-------------

- ``section name`` - Brief 1-2 line explanation 

```