## Simple Inline Flow

Keep logic local and readable in one pass. Prefer direct iteration and immediate error signaling over helper indirection.

Do:

- Keep validation and action in the same function when used once.
- Iterate directly over values.
- Raise immediately with a clear message when a value is invalid.
- Use straightforward control flow over extra abstraction.
- If inline flow is long use comments to separate parts and concisely explain their role

Don't:

- Extract one-off validation into tiny private helpers.
- Add normalization/conversion layers unless required for behavior.
- Introduce extra state/containers that are not needed for correctness.
- Hide the main behavior behind indirection.
- Use private helpers as a way to "section" a long sequence of logic

### Example

```python
# Bad
def _validate(items):
    data = tuple(items)
    if any(not is_valid(x) for x in data):
        raise ValueError("invalid item")
    return data

def process(items):
    for x in _validate(items):
        apply(x)
```

```python
# Good
def process(items):
    for x in items:
        if not is_valid(x):
            raise ValueError("invalid item")
        apply(x)
```

Heuristic:
If the reader has to jump between functions to follow one simple operation, inline it.

## Keep One-Off Constants Local

Define short-lived constants next to the code that consumes them. Local data plus immediate use is easier to read than a top-level list whose purpose is only clear later.

Do:

- Define a list immediately before the operation that uses it.
- Keep each small constant scoped to its real consumer.
- Prefer explicit local groups when a function performs several related steps.

Don't:

- Put one-use constants at module scope by default.
- Separate a list from its only operation with unrelated code.
- Hide simple behavior behind distant shared names.

### Example

```python
# Bad
USER_FIELDS = ("id", "name", "email")
ORDER_FIELDS = ("id", "total", "created_at")

def export_rows(user, order):
    user_row = {field: getattr(user, field) for field in USER_FIELDS}
    order_row = {field: getattr(order, field) for field in ORDER_FIELDS}
    return user_row, order_row
```

```python
# Good
def export_rows(user, order):
    user_fields = ("id", "name", "email")
    user_row = {field: getattr(user, field) for field in user_fields}

    order_fields = ("id", "total", "created_at")
    order_row = {field: getattr(order, field) for field in order_fields}

    return user_row, order_row
```

Heuristic: If a constant has one consumer, define it close enough that the data and its use fit in the same glance.