# Refactoring.Guru code-smell review catalog

Source catalog: [Refactoring.Guru — Code
Smells](https://refactoring.guru/ko/refactoring/smells).

Use this catalog as a diagnostic vocabulary. A smell is not automatically a
bug and does not justify a refactor by itself. Confirm the signal in context,
name the concrete cost, and choose the smallest behavior-preserving remedy.

## Bloaters

| ID | Smell | Confirm when | Typical response and caution |
|---|---|---|---|
| CS01 | Long Method | One routine carries multiple phases, has deep nesting, or exceeds the project's readable size | Extract coherent operations or simplify data flow. Do not create tiny forwarding fragments that hide the algorithm. |
| CS02 | Large Class | A class owns unrelated responsibilities, many fields, or many independent change reasons | Split by responsibility or extract collaborators. A cohesive facade may legitimately be broad. |
| CS03 | Primitive Obsession | Domain concepts repeatedly travel as raw strings, numbers, flags, or loosely related dictionaries | Introduce a value object, enum, or validated type when it centralizes real invariants; avoid wrappers with no behavior or safety benefit. |
| CS04 | Long Parameter List | Callers repeatedly supply many arguments or argument order is easy to misuse | Preserve a whole object or introduce a parameter object. Do not hide unrelated dependencies in a grab-bag context. |
| CS05 | Data Clumps | The same group of values appears together in several signatures, records, or validations | Name the concept and move shared invariants with it. Coincidental co-occurrence is not a domain type. |

## Object-orientation abusers

| ID | Smell | Confirm when | Typical response and caution |
|---|---|---|---|
| CS06 | Alternative Classes with Different Interfaces | Classes perform the same role but force clients to use incompatible operations | Rename and align methods, extract a protocol, or add a narrow adapter. Preserve truly different semantics. |
| CS07 | Refused Bequest | A subtype cannot honor inherited behavior, disables methods, or violates base invariants | Replace inheritance with delegation or correct the hierarchy. Optional framework methods alone are not proof. |
| CS08 | Switch Statements | The same type-code or conditional dispatch is repeated and grows with every variant | Centralize dispatch or use polymorphism/strategy. A single exhaustive branch over a closed, small domain can be clearer. |
| CS09 | Temporary Field | Object fields are meaningful only during one mode or operation and require scattered presence checks | Extract the operation/state object or keep the value local. Lazy caches and explicit optional state may be valid. |

## Change preventers

| ID | Smell | Confirm when | Typical response and caution |
|---|---|---|---|
| CS10 | Divergent Change | One module changes for several unrelated reasons | Separate responsibilities along actual change axes. Do not split a cohesive unit based on hypothetical futures. |
| CS11 | Parallel Inheritance Hierarchies | Adding a subtype in one hierarchy routinely requires a matching subtype elsewhere | Collapse or compose the paired dimensions. Similar names alone do not establish the smell. |
| CS12 | Shotgun Surgery | One conceptual change requires small edits across many modules | Move behavior/data to one owner or create a stable boundary. Cross-cutting requirements may instead need tooling or policy. |

## Dispensables

| ID | Smell | Confirm when | Typical response and caution |
|---|---|---|---|
| CS13 | Comments | A comment compensates for unclear structure, repeats the code, or is stale | Clarify names and structure, then keep comments that explain why, constraints, hazards, or non-obvious decisions. Comments are not inherently a smell. |
| CS14 | Duplicate Code | Equivalent knowledge or algorithms are maintained in multiple places and can drift | Extract shared behavior or parameterize the variation. Similar-looking code with different change reasons may be safer apart. |
| CS15 | Data Class | A class exposes data while behavior and invariants live in its clients | Move relevant behavior/invariants to the owner. DTOs, schemas, events, and immutable records can be intentionally data-only. |
| CS16 | Dead Code | A branch, symbol, parameter, or feature is unreachable or unused | Delete it with associated tests/configuration. Confirm reflective, plugin, serialization, and external entry points first. |
| CS17 | Lazy Class | An abstraction adds navigation and maintenance cost without enough responsibility | Inline or merge it. Keep small classes that enforce a boundary, type distinction, or likely-independent ownership. |
| CS18 | Speculative Generality | Hooks, parameters, layers, or abstractions exist only for imagined use cases | Remove unused flexibility and add it when a concrete need appears. Preserve deliberate public compatibility points. |

## Couplers

| ID | Smell | Confirm when | Typical response and caution |
|---|---|---|---|
| CS19 | Feature Envy | A method mostly reads or manipulates another object's data | Move the method or extract a function near the data owner. Reporting/serialization code may legitimately aggregate foreign data. |
| CS20 | Inappropriate Intimacy | Modules depend on each other's internals, private state, or coordinated mutation | Move behavior, expose a narrow operation, or extract a shared owner. Tests may use controlled seams but should not normalize production coupling. |
| CS21 | Incomplete Library Class | A library lacks behavior and clients repeatedly implement awkward workarounds | Add a local extension, adapter, or upstream contribution. Do not fork or wrap a library for one trivial call. |
| CS22 | Message Chains | Code navigates long object chains and becomes coupled to intermediate structure | Hide the delegate, add a query at the right boundary, or use named intermediate values. Fluent APIs designed as one stable abstraction may be fine. |
| CS23 | Middle Man | A class mostly forwards calls without adding policy, translation, ownership, or stability | Remove the middle layer or call the delegate directly. Keep facades/adapters that intentionally protect a boundary. |

## Review sequence

1. Find changed responsibilities and data ownership.
2. Check bloat and repeated conditional structure locally.
3. Search the affected call sites for repeated argument groups, duplication,
   and scattered changes.
4. Trace dependency direction and object navigation for coupling smells.
5. Confirm each candidate against a real change or failure cost.
6. Prefer a focused refactor with characterization tests; do not mix a broad
   cleanup into an unrelated functional change.

Cross-check overlaps instead of double-reporting them: POT04 often covers
CS01, POT06 can expose CS09/CS20, POT08 can expose CS08, and POT09 often covers
CS22/CS23. Choose the ID that best explains the remedy and mention the overlap
in the same finding.
