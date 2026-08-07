# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A plugin — not an application. It ships guidance and a checker that other
projects load. There is no package to install, no dependencies, and no build
step: everything is stdlib Python 3 and Markdown, because both host agents
execute the files directly from the checkout.

The same `skills/` and `hooks/` directories are consumed by **two hosts**,
Claude Code and Codex, through separate manifests. Any change to skill layout,
hook output shape, or manifest metadata has to stay valid for both.

## Commands

```sh
# Full test suite
python3 -m unittest discover -s tests -v

# One test class, or one test
python3 -m unittest tests.test_audit_python.DocstringTests
python3 -m unittest tests.test_audit_python.DocstringTests.test_detects_each_supported_docstring_style

# Audit changed Python in the working tree (what the Stop hook runs)
python3 skills/review-code-quality/scripts/audit_python.py --git-diff

# Audit explicit paths; this repo must stay clean
python3 skills/review-code-quality/scripts/audit_python.py hooks tests skills

# Manifest validation
claude plugin validate .

# Load the checkout as a live plugin for manual testing
claude --plugin-dir /absolute/path/to/reliable-python
```

There is no linter or type checker configured. `audit_python.py` plus the
unittest suite is the whole validation story — run both before finishing.

### The Stop gate runs the installed plugin, not this checkout

When developing here, the `Stop` hook executes the checker from
`~/.claude/plugins/cache/<marketplace>/reliable-python/<version>/`, so it
audits your changes with the **previously released** rules. A gate finding that
your local checker does not reproduce usually means exactly that — compare the
message wording, which drifts between versions. Run the local checker
explicitly, and when you change a rule's semantics, verify the repository stays
clean under both:

```sh
python3 skills/review-code-quality/scripts/audit_python.py hooks tests skills
python3 ~/.claude/plugins/cache/*/reliable-python/*/skills/review-code-quality/scripts/audit_python.py hooks tests skills
```

Use `/reload-plugins` after changing a directly loaded development plugin.

## Architecture

Three layers, each with a different failure mode to respect.

**1. Session injection (`hooks/session_start.py`).** Reads
`skills/using-power-of-ten/SKILL.md` verbatim and emits it as
`hookSpecificOutput.additionalContext`, so the policy is in context whether or
not the agent chooses to read a skill. It enforces `MAX_ENTRY_SKILL_BYTES =
8_192` on that file. **Adding prose to the entry skill spends a budget charged
to every session** — put detail in a focused skill and link to it. Current size
is about 4.5 KB. This hook fails open: on any error it prints to stderr and
exits 0.

It also appends the resolved docstring convention by shelling out to
`audit_python.py --print-docstring-style --format json`. That indirection is
deliberate: the auditor owns the precedence rules, so guidance and enforcement
cannot disagree. **If you add another configurable setting, resolve it in the
auditor and read it back the same way** rather than parsing configuration twice.

**2. Skills (`skills/*/SKILL.md`).** `using-power-of-ten` is the always-injected
entry policy; the other three are loaded on demand and can be as long as they
need to be. Each also carries `agents/openai.yaml`, which is how Codex renders
it — a new skill needs both files. `references/` under `using-power-of-ten`
holds the two long documents (`power-of-ten.md`, `code-smells.md`) that the
review workflow reads but the session never injects.

**3. Checker (`skills/review-code-quality/scripts/audit_python.py`).** One
dependency-free AST pass, ~1,700 lines. `analyze_source` is the seam worth
knowing: it builds a `ReviewContext` (tree, source lines, path, parent map,
findings list, docstring style) and hands it to nine `_check_*` functions, each
of which reports through `context.report`. Adding a rule means adding a
`_check_*` function and one call there.

**Changed-line filtering is what makes the plugin usable.** `collect_git_diff`
parses `git diff --unified=0` hunk headers into line ranges, and
`filter_changed_findings` keeps only findings whose `line..end_line` span
intersects them. A finding anchored to a node that *encloses* a changed line
still surfaces — that is deliberate, so editing one line inside an unbounded
loop still reports the loop.

**4. Stop gate (`hooks/stop_quality_gate.py`).** Runs the checker with
`--fail-on none`, and returns `{"decision": "block", "reason": ...}` when
findings remain. It allows the second stop unconditionally when
`stop_hook_active` is set — without that the hook would loop forever. Like the
other hook, it fails open on infrastructure errors.

## Rule code namespaces

| Prefix | Meaning |
|---|---|
| `POT01`–`POT10` | Power of Ten, Python profile |
| `CS01`–`CS23` | Refactoring.Guru smells, numbered in catalog order |
| `DOC01`–`DOC03` | Docstring convention; `DOC01`≈numpydoc `GL08`, `DOC03`≈`PR01` |
| `QLT001` | Malformed suppression comment |
| `IO001`, `PARSE001`, `LIMIT001` | Infrastructure, not code-quality, findings |

Every finding carries a `remedy` naming the smallest safe correction. Keep that
property when adding rules — a finding without an actionable remedy is noise.

## Constraints the checker imposes on itself

This repository is audited by its own checker and must stay at zero findings,
which shapes how you write code here:

- **Every loop needs a visible bound.** `for _ in range(MAX_SOMETHING)` with a
  module-level uppercase constant reads as bounded; `for x in some_list` does
  not and needs a suppression. Comprehensions are not analyzed as loops, so
  prefer them for genuinely bounded projections.
- **Functions cap at 60 code lines**, docstrings excluded from the count.
- **Public definitions need docstrings in the configured convention** — NumPy by
  default, resolved from `--docstring-style`, then `RELIABLE_PYTHON_DOCSTYLE`,
  then `[tool.reliable-python] docstring-style` in `pyproject.toml`. The checker
  enforces this on itself. See `skills/writing-docstrings/SKILL.md`.
- **`POT05` anchors its module-wide finding to the file's first function.**
  Adding a function ahead of one that carries a `POT05` suppression moves the
  anchor and re-opens the finding — move the comment with it.
- **Suppressions require a specific rationale**:
  `# quality: ignore[POT02] - payload is rejected above MAX_GIT_OUTPUT_BYTES`.
  The bare form is itself a `QLT001` error. Every `MAX_*` constant exists so
  some loop or allocation can point at it.

## Conventions

Prose in skills and references is deliberately plain: no hype, no claim that
the plugin certifies anything. `README.md` and `references/power-of-ten.md`
both state explicitly that this is not NASA certification and not a safety
proof. Preserve that framing — the Power of Ten rules are C-oriented and the
Python versions of rules 3, 8, and 9 are labeled substitutions, not
equivalents. The docstring convention is documented as a *project convention*
rather than an eleventh rule for the same reason.

Git Flow with unscoped Conventional Commits; `develop` integrates, `main` holds
tagged releases. See `CONTRIBUTING.md`. Version numbers appear in three files
(`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
`.claude-plugin/marketplace.json`) and `PackageConsistencyTests` fails if they
drift — that test hardcodes the expected version, so a release bump edits four
places.
