---
name: writing-docstrings
description: Use when writing or changing any public Python module, class, or function — documents the NumPy docstring convention this project defaults to, and when Google or reST style is the correct choice instead.
---

# Writing Docstrings

**Default to NumPy style.** It handles complex parameter descriptions better
than the alternatives: each parameter gets its own block with room for a type,
a default, constraints, and prose, instead of being squeezed onto one
indented line.

Source: [numpydoc style guide](https://numpydoc.readthedocs.io/en/latest/format.html).

## When to Use

Load this before writing or editing:

- Any public module, class, or function — no leading underscore
- A public method on a public class
- A function whose parameters carry units, ranges, defaults, or invariants
- Any change to an existing docstring's structure

**When NOT to apply:** private helpers, dunders, nested closures, `@overload`
declarations, `...`/`pass` stubs, `@property` setters and deleters, and test
functions in `test_*.py` are all exempt. The checker skips them too. A private
helper with a genuinely subtle contract still deserves a docstring — the
exemption means "not required", not "not allowed".

## Section Order

NumPy fixes the order. Use only the sections you need, but keep them in this
sequence:

1. Short summary
2. Deprecation warning
3. Extended Summary
4. `Parameters`
5. `Returns`
6. `Yields`
7. `Receives`
8. `Other Parameters`
9. `Raises`
10. `Warns`
11. `Warnings`
12. `See Also`
13. `Notes`
14. `References`
15. `Examples`

Classes omit `Returns` and add `Attributes` (directly below `Parameters`) and
an optional `Methods`. Modules use summary, extended summary, routine
listings, `See Also`, `Notes`, `References`, `Examples`.

`Warns` and `Warnings` are **different sections**. `Warns` documents warnings
the code raises; `Warnings` is free-text caution for the reader.

## Core Pattern

Every section heading is underlined with hyphens. Parameters are `name : type`
with the description indented beneath.

```python
def fetch_batch(client, batch_size, timeout_s=30.0, *, retries=3):
    """Fetch one batch of records from the upstream service.

    Blocks until the batch arrives or the retry budget is exhausted. Partial
    batches are returned as-is; the caller decides whether a short batch ends
    the stream.

    Parameters
    ----------
    client : HttpClient
        Configured client. Must already be authenticated.
    batch_size : int
        Number of records to request. Must be in ``1..1000``; the service
        rejects larger values.
    timeout_s : float, default 30.0
        Per-attempt socket timeout in seconds. Does not bound total wall time,
        which is roughly ``timeout_s * retries``.
    retries : int, default 3
        Attempts before giving up, counting the first attempt.

    Returns
    -------
    list of Record
        Between zero and `batch_size` records, in service order.

    Raises
    ------
    ValueError
        If `batch_size` falls outside the accepted range.
    TimeoutError
        If every attempt exceeds `timeout_s`.
    """
```

Note what the parameter blocks carry that a one-liner cannot: the accepted
range for `batch_size`, the fact that `timeout_s` is *per attempt* rather than
total, and the off-by-one clarification that `retries` counts the first
attempt. That is the reason for the default.

## Parameter Syntax

| Form | Meaning |
|---|---|
| `x : int` | Name, space, colon, space, type. The space before the colon is required. |
| `x` | Type omitted — then omit the colon too. |
| `x : int, optional` | Optional with no documented default. |
| `copy : bool, default True` | Optional with a stated default. |
| `order : {'C', 'F', 'A'}` | Fixed set of choices; first is the default. |
| `x1, x2 : array_like` | Two parameters sharing one description. |
| `*args`, `**kwargs` | Written without a type. |

The summary line is one line, ends with a period, and does not repeat the
function's own name.

## The Alternates

Google and reST are acceptable, under one condition: **match the file you are
editing.** A file already written in Google style stays in Google style — a
mixed-convention file is worse than either convention.

```python
# Google — compact, but the type and prose share one line
"""
Args:
    batch_size (int): Number of records; must be in 1..1000.
"""

# reST — Sphinx-native field lists
"""
:param int batch_size: Number of records; must be in 1..1000.
"""
```

Switching a whole project is a deliberate decision, not a side effect of one
edit. Record it in the project's `pyproject.toml`, so every session, teammate,
and CI run agrees without anyone remembering a flag:

```toml
[tool.reliable-python]
docstring-style = "google"   # numpy (default), google, rest, or any
```

Precedence is `--docstring-style` → `RELIABLE_PYTHON_DOCSTYLE` →
`pyproject.toml` → `numpy`. Check what is actually in effect with:

```sh
python3 <skill-directory>/../review-code-quality/scripts/audit_python.py --print-docstring-style
```

The resolved convention is also stated at the end of the injected session
policy, so it is visible without running anything.

## Checker Rules

| ID | Fires when | numpydoc equivalent |
|---|---|---|
| `DOC01` | A public module, class, or function has no docstring | `GL08` |
| `DOC02` | The sections identify a style other than the configured one | — |
| `DOC03` | A parameter section exists but documents only some parameters | `PR01` |

`DOC02` never fires on a summary-only docstring. One line with no sections is
valid NumPy, valid Google, and valid reST, so there is nothing to disagree
with. The rule fires only on positive evidence of a competing convention.

Use this project's suppression form, not numpydoc's `# numpydoc ignore=`:

```python
# quality: ignore[DOC01] - re-exported for backward compatibility only
def legacy_entry_point(payload):
    return parse(payload)
```

## Common Mistakes

**Missing the space before the colon.** `x: int` is not NumPy — the spec
requires `x : int`. Without the space, tooling reads the whole line as a name.

**Underline shorter than the heading.** `Parameters` needs its hyphens; a
truncated underline silently drops the section from rendered docs.

**Documenting some parameters.** A `Parameters` section listing three of five
reads as complete and is worse than no section at all. That is `DOC03`.

**Restating the signature.** `alpha : int — the alpha value` costs a line and
adds nothing. Document the constraint, the unit, or the invariant, or leave
the parameter out of the docstring and let the type annotation speak.

**Writing `Returns` for a generator.** Generators use `Yields`. If it also
consumes `.send()` values, add `Receives` below `Yields`.

## Rationalizations

| Excuse | Reality |
|--------|---------|
| "The type annotation already says it" | The annotation says `int`. It does not say "must be 1..1000, the service rejects more". |
| "It's obvious from the name" | `timeout_s` does not say whether it bounds one attempt or the whole call. That ambiguity is a bug report waiting to happen. |
| "NumPy style is too verbose here" | Then the function has few enough parameters that the summary line alone is fine. Verbosity is per-parameter, and you pay only for parameters you have. |
| "I'll document it once the API settles" | The docstring is how you find out whether it has settled. Undocumented parameters are where the accidental coupling hides. |
| "Google style is more readable" | It is, for `name (type): one short clause`. It stops being readable the moment a parameter needs three sentences and a default. |
