# Docstring Schema

## 1. Overview

Docstrings must follow:

* NumPy-style section headers
* fixed section order
* required `Parameters` and `Returns`
* structured `Examples` with named subsections
* typed `See Also` entries

---

# 2. Sections (compact specification)

* **Summary line** *(required)*
  One sentence describing the object.

* **Extended summary** *(include if non-trivial)*
  Include if behavior, abstraction, or scope is not obvious. Maybe up to 1-3 paragraphs.

* **Parameters** *(required)*
  Must always be present with brief description for each entry. Use `None` if no parameters exist.

* **Returns** *(required for functions; forbidden for classes and modules)*
  Must always be present for functions with brief description. Use `None` if nothing is returned.

* **Attributes** *(required for classes; forbidden for non-classes;)*
  Must include all user-facing attributes with brief description for each entry. Use `None` if there are no public attributes 

* **Raises** *(include if user-relevant exceptions exist)*
  Include only for intentional exceptions.

* **Notes** *(include if non-obvious semantics exist)*
  Include for invariants, constraints, or important behavior.

* **Examples** *(required for public classes and functions except trivial wrappers)*
  Must contain named subsections with executable examples.

* **See Also** *(include if at least one meaningful related target exists)*
  Use `Target : <type> description`.

---

# 3. Details

## 3.1 Summary line

Exactly one sentence describing the object.

* declarative
* no filler or tutorial phrasing
* no multiple sentences

---

## 3.2 Extended summary

Include when:
* abstraction is non-obvious
* behavior needs clarification
* interaction model is important

Content: 
1–3 short paragraphs explaining
* mental model
* scope
* relationship to adjacent APIs

Do not include
* examples
* parameter descriptions
* step-by-step instructions

---

## 3.3 Parameters

* all public parameters must be listed
* include type for each parameter
* describe semantics (not just name)
* if none exist, use exactly `None`

### Format (with parameters)

```text
Parameters
----------
name : type
    description
```

### Format (no parameters)

```text
Parameters
----------
None
```
---

## 3.4 Returns

* must always be present in functions
* describe meaning, not just type
* if no return value, use exactly `None`

### Format (with return value)

```text
Returns
-------
type
    description
```

### Format (no return value)

```text
Returns
-------
None
```

---

## 3.5 Attributes

* only public attributes
* exclude internal state

Add comment if class has slots and therefore forbids user-defined attributes.

### Format

```text
Attributes
----------
name : type
    description
```
---

## 3.6 Raises

Include when object intentionally raises exceptions users should handle
* include only contract-level exceptions
* omit incidental internal errors

### Format

```text
Raises
------
ExceptionType
    description
```

---

## 3.7 Notes

Include when important semantics are not obvious from examples
Cover:
* invariants
* guarantees
* limitations
* interoperability notes

Do not include
* filler commentary
* tutorials
* irrelevant implementation detail

---

## 3.8 Examples

Required for all public classes and functions except trivial wrappers.

Must contain named subsections. 
* each subsection must contain executable doctest-style code
* examples must show output
* no assertions
* no long narrative

Allowed subsection headings:

* Basic usage
* Composition
* Variations
* Edge cases
* Interoperability
* Advanced usage


### Format
```text
Examples
--------
Basic usage:

>>> ...

Composition:

>>> ...
```
---

The composition section describes use of this object together with other objects to produce a larger result.
Examples in `Composition` must involve at least one additional component. Multiple `Composition` examples may be present, grouped under Composition section, separated by plain text

```text
Composition:

	with A

>>> ...

	with B

>>> ...

```
---

## 3.9 See Also

Include when at least one concrete related object exists

* left-hand side must be a bare target
* `<type>` must be first element in description
* description must explain relationship
* 2–6 entries typical

### Allowed `<type>`

* <class>
* <function>
* <module>
* <package>
* <notebook>
* <external-class>
* <external-function>
* <external-module>
* <doc> - used also for external urls. In that case Target is the name of the resource and optional continuoation is mandatory and must contain start with url.


Do not include
* `help(...)`
* markdown formatting
* vague references
* type metadata on left-hand side

### Format

```text
Target : <type> description.
    optional continuation
```
---

# 4. Section order

## 4.1 Functions

```text
Summary
Extended summary
Parameters
Returns
Raises
Notes
Examples
See Also
```

---

## 4.2 Classes

```text
Summary
Extended summary
Parameters
Attributes
Raises
Notes
Examples
See Also
```

---

## 4.3 Modules

```text
Summary
Extended summary
Notes
Examples
See Also
```

---

# 5. Minimal valid forms

## Function

```python
"""
One-sentence summary.

Parameters
----------
None

Returns
-------
None

Examples
--------
Basic usage:

>>> ...
"""
```

---

## Class

```python
"""
One-sentence summary.

Parameters
----------
None

Examples
--------
Basic usage:

>>> ...
"""
```

---

## Module

```python
"""
One-sentence summary.
"""
```

---

# 6. Full example

```python
class SymbolFamily:
    """
    Create a family of indexed symbolic variables.

    A SymbolFamily provides indexed access for constructing related
    symbolic variables while remaining compatible with larger workflows.

    Parameters
    ----------
    name : str
        Base name used when constructing indexed symbols.

    Notes
    -----
    Each indexed access produces a symbolic variable derived from the
    family name and the provided index.

    Examples
    --------
    Basic usage:

    >>> x = SymbolFamily("x")
    >>> x[1]
    x_1

    Variations:

    >>> x["i"]
    x_i

    Composition:

    >>> x = SymbolFamily("x")
    >>> f = FunctionFamily("f")
    >>> f[1](x[1])
    f_1(x_1)

    See Also
    --------
    FunctionFamily : <class> Indexed symbolic functions.
        Use when indexed objects should be callable.

    symbols : <function> Direct symbol construction without family wrappers.
    """
```

