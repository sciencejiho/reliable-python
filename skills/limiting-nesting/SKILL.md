---
name: limiting-nesting
description: Use when writing or reviewing any Python function that nests blocks — if inside for inside try, guard pyramids, arrow-shaped code. Keeps block depth at four levels or fewer by extracting units and returning early.
---

# Limiting Nesting

**Keep a function at four levels of block nesting or fewer.** The function body
is level one. Past level four, extract a named unit or invert a condition.

Depth is not a style preference. Each level is a condition the reader must hold
in their head to know whether a line runs at all. At five levels the reader is
tracking five simultaneous truths, and the line that finally does the work is
40 characters from the left margin.

## When to Use

Load this before writing or editing:

- Any `if` inside a `for` inside a `try`
- Validation that grows a new `if` per field
- A loop body that filters, then branches, then acts
- Any block whose closing lines are a run of dedents
- Code where the real work sits at the far right of the screen

**When NOT to apply:** depth is a signal, not a defect. A parser or state
machine with a genuinely four-deep decision structure is fine at four. The rule
draws the line where a reader stops being able to enumerate the conditions, not
where the code stops being pretty.

## Core Pattern

```python
# Nests 5 levels: for -> if -> for -> if -> the actual work
def collect_errors(reports):
    findings = []
    for report in reports:
        if report.enabled:
            for entry in report.entries:
                if entry.severity == "error":
                    findings.append(entry)
    return findings
```

Two independent fixes. **Invert to return early**, which removes a level at the
top:

```python
def collect_errors(reports):
    findings = []
    for report in reports:
        if not report.enabled:
            continue
        for entry in report.entries:
            if entry.severity == "error":
                findings.append(entry)
    return findings
```

Or **extract the inner unit**, which gives the inner logic a name:

```python
def _error_entries(report):
    return [entry for entry in report.entries if entry.severity == "error"]


def collect_errors(reports):
    findings = []
    for report in reports:
        if report.enabled:
            findings.extend(_error_entries(report))
    return findings
```

The second is usually better: `_error_entries` is independently testable, and
the outer function now reads as one sentence.

## Techniques

| Shape | Fix |
|---|---|
| `if cond:` wrapping the whole body | Invert it — `if not cond: return` |
| `if` inside a loop wrapping the body | `if not cond: continue` |
| Nested loop doing its own filtering | Extract the inner loop as a named function |
| `try` wrapping a large block | Wrap only the call that raises |
| `if/elif` ladder | Already flat — an `elif` chain counts as one level |
| Filter, then branch, then act | Comprehension for the filter, loop for the act |
| Deep `if` on optional fields | Validate and return early at the top of the function |

An `elif` ladder is deliberately **not** a violation. `if/elif/elif/else`
reads as one flat decision, and the checker counts it as one level even though
the AST nests it.

## Common Mistakes

**Extracting a fragment instead of a unit.** Pulling out three lines that need
five parameters and only make sense at that one call site trades nesting for
coupling. If you cannot name the extracted function without saying "part two",
invert a condition instead.

**Flattening by combining conditions.** `if a and b and c and d:` is one level
and unreadable. The nesting was a symptom; a four-clause condition is the same
complexity wearing a different shape.

**Hiding depth in a comprehension.** A triple-nested comprehension is not
flatter than the loop it replaced, it is the same depth with fewer places to
put a name.

## Rationalizations

| Excuse | Reality |
|--------|---------|
| "It's only one level over" | Then one `continue` fixes it. The cheapness of the fix is the argument for making it. |
| "Extracting would hurt performance" | A Python function call is nanoseconds. Measure before trading readability for it. |
| "The logic genuinely is nested" | Genuinely nested logic is exactly what deserves named intermediate steps. |
| "It's a short function" | Depth and length are different problems. A 12-line function can be six levels deep. |
| "I'd need to pass too many arguments" | That is the finding, not an objection to it. High argument count means the block was reading state it should not have been. |

## Red Flags — Stop and Flatten

- The closing lines of a function are a staircase of dedents
- You scroll right to read the working line
- A loop body is entirely one `if`
- You are about to add a level to code that already has four
- A block's purpose needs a comment because its conditions are off-screen

## Checker Rule

Reported as `CS01` (Long Method), whose catalog signal explicitly includes deep
nesting. `POT04` covers the other half — a function over 60 code lines — so the
two together cover both ways a function becomes hard to hold in the head.

```text
CS01: function collect_errors nests 5 levels deep (limit: 4)
  remedy: Extract the inner block into a named function, or invert a
          condition to return early and remove a level.
```

The finding anchors to the deepest statement, not the function definition, so
it points at the line that needs to move.
