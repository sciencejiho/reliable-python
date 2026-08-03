# Power of Ten: source rules and Python profile

Primary source: Gerard J. Holzmann, [The Power of Ten — Rules for Developing
Safety Critical Code](https://spinroot.com/gerard/pdf/P10.pdf), NASA/JPL
Laboratory for Reliable Software (2006).

The paper was produced at JPL under a NASA contract and primarily targets C.
Treat it as a strict reliability guideline, not a claim that this plugin,
Python, or code reviewed with it is NASA-certified or suitable for a regulated
safety case.

## Interpretation contract

- Preserve the original rule when Python has a direct analogue.
- Label an intent-preserving substitution as the **Python profile**.
- Prefer analyzability and explicit bounds over cleverness.
- Apply findings to changed code and affected behavior. Do not force unrelated
  repository-wide cleanup.
- Require an explicit, reasoned waiver when a rule cannot be satisfied.

## POT01 — Simple control flow

**Source rule.** Restrict control flow to simple constructs; prohibit `goto`,
`setjmp`/`longjmp`, and direct or indirect recursion.

**Python profile.** Prohibit direct and mutual recursion. Replace it with an
explicit worklist whose depth and total work are bounded. Use exceptions for
exceptional failures, not routine loop exits or multi-level branching. Early
error returns are acceptable when they make control flow clearer.

**Review evidence.** Build or inspect the affected call graph, search for
self-calls and cycles, and trace exception paths. A callback or dynamic dispatch
that hides a cycle is still recursion.

## POT02 — Fixed loop bounds

**Source rule.** Every terminating loop must have a preset upper bound that a
static checker can prove cannot be exceeded. A deliberately non-terminating
scheduler loop must instead be provably non-terminating.

**Python profile.** Put named ceilings on polling, retries, pagination,
convergence, generators, queue draining, and input traversal. A finite
container is not enough when its maximum size is itself unbounded. Fail
explicitly when the ceiling is reached.

**Review evidence.** Identify the bound, where it is established, why it is
finite, and what happens at exhaustion. Reject counters that can reset or fail
to advance.

## POT03 — No dynamic allocation after initialization

**Source rule.** Do not allocate dynamic memory after initialization; operate
within preallocated memory so maximum use is statically knowable.

**Python profile.** Literal compliance is generally unavailable because Python
allocates dynamically. Preserve the intent by setting enforceable limits for
all input-dependent resource growth: collection sizes, buffers, queues, caches,
tasks, threads, processes, file sizes, response bodies, and retained objects.
Use streaming or bounded containers where practical.

**Review evidence.** State a worst-case resource ceiling. Flag `.append`,
`.extend`, cache insertion, task creation, or accumulation inside loops when no
upstream or local cap is visible.

## POT04 — Small functions

**Source rule.** Keep a function to one printed page in standard formatting,
typically about 60 lines with one statement or declaration per line.

**Python profile.** Use 60 physical source lines from `def`/`async def` through
the function body as a review threshold. Do not evade it with semicolons,
compressed expressions, or a large nested function. Extract only coherent,
well-named units.

**Review evidence.** Measure the function and confirm it represents one logical
unit. Line count is a guardrail; clarity is the objective.

## POT05 — Assertion density

**Source rule.** Average at least two meaningful assertions per function.
Assertions must be side-effect-free Boolean checks, must not be statically
always true or false, and must trigger an explicit recovery action.

**Python profile.** Count meaningful preconditions, postconditions, invariants,
and checked assumptions across non-trivial functions. Prefer explicit
guard-and-raise or checked-result logic for production invariants, because
Python removes `assert` statements when optimization is enabled. Never add
`assert True`, tautologies, or checks whose result is ignored.

**Review evidence.** Inspect assertion density across the changed unit, not as
an inflexible quota on trivial accessors. Verify each check has no side effect
and that failure is handled deliberately.

## POT06 — Narrow data scope

**Source rule.** Declare each data object at the smallest possible scope.

**Python profile.** Keep state local, make ownership explicit, and avoid
`global`, mutable module singletons, mutable default arguments, and variables
reused for unrelated meanings. Module constants and deliberately shared,
encapsulated immutable values are acceptable.

**Review evidence.** Trace every mutation site and reduce the set of code that
can observe or corrupt the value.

## POT07 — Check results and parameters

**Source rule.** Each caller checks non-void return values, and each function
validates its parameters. An intentionally ignored result must be made explicit
and justified.

**Python profile.** Validate data at public, trust, parsing, and I/O boundaries.
Handle optional values, status objects, subprocess results, write counts,
transactions, and other fallible outcomes. Propagate errors with context. Do
not use a bare `except`, `except: pass`, or an empty broad handler.

**Review evidence.** Follow failure results through callers. Distinguish a
function that cannot usefully validate an internal typed invariant from a
boundary that accepts untrusted or dynamically typed data.

## POT08 — Restrict the preprocessor

**Source rule.** In C, limit the preprocessor to header inclusion and simple,
complete macro units; avoid token pasting, recursive or variadic macros, and
almost all conditional compilation.

**Python profile.** Literal preprocessor compliance is not applicable. Preserve
analyzability by prohibiting `eval`, `exec`, runtime source generation, dynamic
imports, monkey-patching, and unjustified `getattr`/`setattr` dispatch. Prefer
explicit tables, typed protocols, and ordinary functions.

**Review evidence.** Confirm that static tools and a human can see the executed
code and enumerate configuration variants.

## POT09 — Restrict pointers and indirection

**Source rule.** In C, allow at most one pointer dereference level, do not hide
dereferences in macros or typedefs, and prohibit function pointers.

**Python profile.** Literal pointer compliance is not applicable. Preserve
local reasoning with shallow object navigation, explicit dependencies, named
intermediate values, typed callables, and visible dispatch. Treat long message
chains, service locators, opaque callback registries, and reflection as risks.

**Review evidence.** Ask whether a reader can identify the callee, data owner,
and possible failure at one boundary. Do not blindly reject normal attribute
access or a justified strategy object.

## POT10 — Zero warnings and routine static analysis

**Source rule.** From the first day, enable the compiler's most pedantic
warnings, compile with zero warnings, and run at least one modern static analyzer
daily with zero warnings.

**Python profile.** Run the repository's formatter/check mode, linter, strict
type checker, security/static analyzer, and tests as configured. Do not invent
tooling when a project already defines it. Fix the code that confuses a tool;
use a narrow suppression only for a proven false positive with a rationale.

**Review evidence.** Record exact commands and results. “The warning is probably
wrong” is not evidence.

## Waiver format

Use this only for checker limitations or a deliberate, approved design:

```python
# quality: ignore[POT09] - framework callback is the typed public extension point
registry.register(handler)
```

Keep the waiver adjacent to the relevant node, use one ID, and explain the
specific invariant or boundary that makes the exception safe. A waiver records
a decision; it does not turn non-compliant code into compliant code.
