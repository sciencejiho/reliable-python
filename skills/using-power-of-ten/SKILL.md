---
name: using-power-of-ten
description: Apply NASA/JPL's Power of Ten rules and the Refactoring.Guru code-smell catalog while writing, changing, reviewing, or refactoring Python. Use for any Python implementation task in a project that has adopted strict reliability or maintainability guardrails.
---

# Reliable Python

Treat these rules as constraints while designing and editing code, then verify
the finished diff. Optimize for code whose resource use, control flow, and
failure modes a reviewer can enumerate.

## Scope and honesty

Apply an intent-preserving Python profile of Gerard Holzmann's C-oriented
Power of Ten rules. Do not describe the result as literal C compliance, NASA
certification, or proof of safety. Rules 3, 8, and 9 require language-specific
translation; read [the rule mapping](references/power-of-ten.md) whenever they
are relevant.

Treat a code smell as a design signal, not an automatic defect. Confirm a
concrete cost—harder change, hidden failure, duplication, or excess coupling—
before refactoring. Read [the smell catalog](references/code-smells.md) for any
structural change or review.

## Working rules

1. Keep control flow simple. Do not use direct or indirect recursion. Do not
   use exceptions for ordinary branching.
2. Give every terminating loop a preset upper bound. Make an intentionally
   non-terminating service loop explicit and keep bounded work inside it.
3. Bound runtime growth. Put ceilings on collections, queues, caches, input
   sizes, retries, concurrency, and other resources.
4. Keep each function at about 60 physical lines or fewer. Extract coherent
   units instead of compressing statements.
5. Maintain at least two meaningful, side-effect-free defensive checks per
   function on average. Prefer explicit guard-and-raise logic for production
   invariants because Python can remove `assert` statements with `-O`.
6. Declare and mutate data in the narrowest useful scope. Avoid mutable
   globals, `global`, and mutable default arguments.
7. Validate public-boundary parameters and handle fallible results. Never
   swallow an error with `except: pass` or discard a status accidentally.
8. Avoid behavior hidden from static analysis: `eval`, `exec`, dynamic imports,
   runtime code generation, and unjustified attribute magic.
9. Keep indirection shallow. Replace long message chains and opaque callback
   dispatch with named, typed boundaries.
10. Run the project's strictest compiler, linter, type checker, and tests. End
    with zero warnings in the changed scope; fix confusing code instead of
    silencing tools without a written reason.

## Documentation convention

This is a project convention, not one of Holzmann's ten rules. Document every
public module, class, and function in this project's configured convention,
which defaults to **NumPy style**; it carries per-parameter types, defaults, and
constraints that a reader needs to enumerate a function's inputs. Google and
reST are supported alternates, set per project. The active convention is stated
at the end of this policy. Load `writing-docstrings` before writing or
restructuring a docstring.

## Workflow

Before editing, inspect repository guidance and existing validation commands.
Load `bounded-loops` before implementing retry, polling, pagination,
convergence, stream-consumption, or other variable-length loops.

While editing:

- Prefer the smallest design that satisfies the requirement.
- Preserve behavior unless the user requests a behavior change.
- Address smells in touched code when the benefit is concrete and the change
  stays in scope; do not launch unrelated cleanup.
- Never hide a violation. If compliance is infeasible, explain the tradeoff
  and obtain an explicit decision.

Before finishing:

1. Inspect the complete diff and trace affected callers, tests, and failure
   paths.
2. Invoke `review-code-quality` for a structured semantic review.
3. Run its bundled Python checker on changed Python files.
4. Run the repository's relevant lint, type-check, and test commands.
5. Fix confirmed findings. Report remaining exceptions with rule or smell ID,
   evidence, impact, and rationale.

Use a narrow suppression only for a confirmed false positive:

```python
# quality: ignore[POT02] - process scheduler is intentionally non-terminating
while True:
    run_one_bounded_cycle()
```

Require a specific rationale after the marker. Never add a suppression merely
to make the checker pass.
