# Docstring Style

This file controls docstrings. Use `docs/python_rules.md` for code
organization and inline comments.

Docstrings document the public contract, intended use, and important behavior
of modules, classes, functions, and methods. They should not be source-code
tours.

## Core rules

- Use NumPy-style section headers.
- Start every docstring with exactly one declarative summary sentence.
- Describe observable behavior, inputs, outputs, side effects, guarantees, and
  limitations.
- Mention important public entrypoints when that helps users know how to call
  the module or object.
- Do not describe the order of definitions in the file.
- Do not use docstrings to compensate for missing block comments inside code.


## Module docstrings

Module docstrings describe the module's public responsibility, intended use,
and general implementation approach. They may name public entrypoints, but
only to explain how users interact with the module.

For substantive public modules, use this order:

1. Summary: one sentence describing the module's capability.
2. Extended summary: one paragraph describing important behavior, scope,
   inputs and outputs, side effects, generated files, safety checks, or
   limitations.
3. Usage and API: one paragraph describing the public entrypoints (Python module import, command line, etc.).
4. Implementation Approach: a descrition of the high-level strategy
   used to provide the module's behavior.
5. See Also: (optional) when another public module or function is essential
   context.

Trivial wrapper modules, package ``__init__.py`` files, and small internal
modules may use a summary alone or reference the main package when the additional parts would not convey meaningful information.

The `Usage and API` paragraph identifies public entrypoints and their intended
roles. 

`Implementation Approach` is an architectural overview, not a source-code
tour. Describe concepts such as the parsing or traversal model, transformation
boundaries, preservation rules, shared infrastructure, safety model, or the
steps that establish the module's key guarantees. Do not describe definition
order, direct readers to a location in the file, enumerate private helpers, or
narrate private call chains.

Use this template:

```python
"""Brief declarative description of the module's capability.

Describe the module's observable behavior, scope, important guarantees, and
limitations.

Usage and API
-------------
Call ``public_function(...)`` from Python. The command-line interface delegates
to ``main(...)`` for file, clipboard, or standard-stream workflows.

Implementation Approach
-----------------------
Describe the high-level representation, selection, transformation, and
preservation strategy that makes the public behavior reliable.
"""
```

Do not include `Parameters`, `Returns`, or `Raises` sections in ordinary module
docstrings.

Bad:

```python
"""Copy declared pipeline inputs into a managed build directory.

Start with ``run(...)`` for the library workflow and ``main(...)`` for the CLI
wrapper. The support helpers below expand inputs, resolve output paths, and
perform the copies.
"""
```

Good:

```python
"""Convert Unicode Greek letters in LaTeX math to LaTeX macros.

Only recognized Greek letters in parsed math delimiters and supported math
environments are converted; prose, preambles, and unsupported regions remain
unchanged.

Usage and API
-------------
Call ``transform_text(...)`` to transform a LaTeX fragment or document from
Python. The command-line interface delegates to ``main(...)`` for file,
clipboard, and standard-stream workflows.

Implementation Approach
-----------------------
The module parses the source, selects supported math regions, replaces each
recognized glyph through a shared mapping, and reconstructs every other region
without modification.
"""
```

Avoid source-navigation language, especially:

- "Start with ..."
- "read ... first"
- "helpers below"
- "below them"
- "remaining sections"
- "implementation stays ..."
- "the workflow comes first"
- "support helpers below"
- "low-level details below"

Architectural language is encouraged when it explains the module's strategy
without becoming source navigation:

- "The module parses supported math regions before applying replacements."
- "Inputs are validated before outputs are created to avoid partial results."
- "A shared mapping preserves distinct spellings for visually similar glyphs."

Usage-oriented entrypoint mentions are fine:

- "Call ``run(...)`` to copy inputs into the build directory."
- "The command-line interface delegates to ``main(...)``."
- "Most callers should use ``freeze_numbers(...)`` rather than lower-level
  manifest helpers."

## Public functions and methods

Use this section order:

1. Summary, required.
2. Extended summary, optional.
3. Parameters, required when the callable accepts public parameters.
4. Returns, required when the callable returns a public value.
5. Raises, required for intentional exceptions raised by the callable.
6. Notes, optional.
7. Examples, required for substantive public APIs and optional for trivial
   wrappers.
8. See Also, optional.

Function and method docstrings should describe the public contract of the
callable. Put algorithm narration in code comments, not in the docstring.

## Public classes

Use this section order:

1. Summary, required.
2. Extended summary, optional.
3. Parameters, required when construction accepts public parameters.
4. Methods, required when the class exposes meaningful public methods.
5. Attributes, required when the class exposes public attributes or properties.
6. Raises, required for intentional construction-time exceptions.
7. Notes, optional.
8. Examples, required for substantive public classes and optional for trivial
   wrappers.
9. See Also, optional.

A class docstring should document the class as an object a user can construct
and use. It should not duplicate every method docstring.

## Section reference

### Summary

Exactly one declarative sentence describing the object.

Do not use filler, tutorial phrasing, or multiple sentences.

### Extended summary

Use only when the abstraction, behavior, scope, or interaction model is not
obvious from the summary.

Keep it to one to three short paragraphs. Cover the mental model, scope,
relationship to adjacent APIs, important guarantees, or limitations. Do not
include parameter lists or examples.

### Parameters

List every public parameter.

Format:

```text
Parameters
----------
name : type
    Description of the parameter's meaning and accepted values.
```

Describe semantics, not just the parameter name.

### Returns

Document the meaning of the return value, not just its type.

Format:

```text
Returns
-------
type
    Description of the returned value.
```


### Methods

List public methods that are important to the class interface. Omit inherited,
private, or incidental methods.

Format:

```text
Methods
-------
method_name
    Description of the method's role in the class interface.
```

### Attributes

List public attributes and properties. Exclude internal state.

Format:

```text
Attributes
----------
name : type
    Description of the attribute's meaning.
```

If a class intentionally exposes no public attributes, omit this section.

### Raises

Include intentional exceptions that are part of the public or internal
contract. Do not list exceptions that merely originate from unrelated lower
layers unless the object deliberately exposes them.

Format:

```text
Raises
------
ExceptionType
    Condition that causes the exception.
```

### Notes

Use `Notes` for important semantics that do not belong elsewhere:

- invariants,
- guarantees,
- limitations,
- constraints,
- side effects,
- ordering requirements,
- interoperability notes,
- reasons behavior should not be simplified.

Do not use `Notes` for filler, tutorials, or irrelevant implementation detail.

### Examples

Examples should be executable doctest-style snippets with shown output.

Rules:

- Use named subsections such as `Basic usage`, `Composition`, `Variations`,
  `Edge cases`, `Interoperability`, or `Advanced usage`.
- Show output.
- Do not use assertions as the only visible result.
- Keep narrative short.

Base format:

```text
Examples
--------
Basic usage:

>>> obj = make_object()
>>> obj.value
42

Composition:

>>> result = compose(obj)
>>> result.name
'example'
```

### See Also

Include when at least one meaningful related target exists.

Rules:

- Use a bare target on the left-hand side.
- Put the target type first in the description.
- Explain the relationship.
- Use two to six entries when the section is present.

Allowed target types:

- `<class>`
- `<function>`
- `<module>`
- `<package>`
- `<notebook>`
- `<external-class>`
- `<external-function>`
- `<external-module>`
- `<doc>`

Format:

```text
Target : <type> Description of the relationship.
    Optional continuation line.
```

For external URLs, use `<doc>` and start the continuation line with the URL.
Do not include `help(...)`, Markdown formatting, vague references, or type
metadata on the left-hand side.

## Private and internal docstrings

Private and internal objects include names beginning with `_`,
implementation-only classes, internal modules, and helpers outside the public
API.

Private helpers are allowed only when they earn their existence. Prefer inline
code when it is clearer, keep one-off validation local, and avoid helper
indirection that forces the reader to jump around to understand simple
behavior.

If a private object exists, its docstring may be brief, but it must state the
helper's internal role.

Use this lightweight order when sections are needed:

1. Summary, required.
2. Extended summary, optional for nontrivial helpers.
3. Parameters, optional when parameter meaning is not immediate.
4. Returns, optional when the return value has non-obvious meaning.
5. Raises, required for intentional internal exceptions.
6. Notes, optional for internal constraints, assumptions, side effects, or
   compatibility quirks.
7. Examples, rare; use only for private parsers, renderers, mini-protocols, or
   tricky edge cases.
8. See Also, rare; use only when another helper, test, public API, or design
   document is essential context.

Minimal example:

```python
def _declared_doc(obj: Any) -> dict[str, str | None] | None:
    """Return documentation metadata declared directly on an object or its type."""
```
