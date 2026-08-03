---
name: review-code-quality
description: Audit Python source, diffs, pull requests, or refactors for NASA/JPL Power of Ten violations and actionable Refactoring.Guru code smells. Use when reviewing code quality, validating a completed Python change, investigating maintainability risks, or fixing reliability findings.
---

# Review Code Quality

Produce an evidence-based review of the changed scope. Separate mechanical
rule violations from heuristic design smells and avoid speculative findings.

## Review workflow

1. Read `../using-power-of-ten/references/power-of-ten.md` and
   `../using-power-of-ten/references/code-smells.md`.
2. Read repository guidance and identify the requested change, affected public
   behavior, and changed lines. Review the diff first; open surrounding code
   only as needed to prove a finding.
3. Run the bundled checker from the repository root:

   ```bash
   python3 <skill-directory>/scripts/audit_python.py --git-diff
   ```

   For explicit files or directories, replace `--git-diff` with their paths.
   The checker covers only high-signal, statically detectable cases; it does
   not replace the semantic pass.
4. Trace the ten rules in order. For Rules 3, 8, and 9, label conclusions as
   Python-profile findings rather than literal C-rule compliance.
5. Review the touched design against all five smell families. Report a smell
   only when the code shows the catalog's signal and a concrete maintenance or
   correctness cost.
6. Run the repository's own formatter, linter, type checker, and focused tests.
   Do not substitute the bundled checker for project validation.
7. If asked to fix findings, make the smallest behavior-preserving change,
   rerun checks, and re-review the resulting diff.

## Finding standard

Report only findings that are actionable and caused or exposed by the changed
scope. Use:

```text
[severity] ID — concise title
location: path:line
evidence: what the code demonstrably does
impact: concrete failure or maintenance cost
remedy: smallest safe correction
```

Use `error` for a clear rule violation or correctness risk, `warning` for a
confirmed smell with material cost, and `note` for an explicit, justified
tradeoff. Do not report style preferences, existing unrelated debt, or a smell
name without evidence.

If no findings remain, say so and list the checks actually run. Never imply
that this review proves safety or exhaustively detects every smell.

## Suppressions

Honor `# quality: ignore[ID] - rationale` only when it is on the finding line or
immediately above the relevant statement and contains a specific rationale.
Challenge broad, stale, or circular rationales. Prefer correcting code over
adding a suppression.
