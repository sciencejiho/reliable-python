# reliable-python

A shared Codex and Claude Code plugin for writing and reviewing Python with:

- Gerard Holzmann's [Power of Ten](https://spinroot.com/gerard/pdf/P10.pdf)
  rules, translated into an explicitly labeled Python profile; and
- the 23 smells in the
  [Refactoring.Guru catalog](https://refactoring.guru/ko/refactoring/smells).

The original rules target C and safety-critical development. This plugin does
not certify Python code, prove safety, or claim to be an official NASA standard.
It makes the rules hard to overlook and mechanically checks the subset that can
be detected with useful confidence.

## How it works

The same `skills/` and `hooks/` directories are loaded by both hosts.

1. A `SessionStart` hook injects the compact reliable-Python policy at startup,
   resume, clear, and compaction.
2. The `using-power-of-ten` skill guides implementation continuously.
3. The `review-code-quality` skill performs an evidence-based semantic review
   across all ten rules and all five smell families.
4. Its dependency-free AST checker audits changed Python lines.
5. A `Stop` hook asks the agent for one more pass when the checker reports a
   changed-scope finding. It then allows a second stop to prevent a hook loop.

The checker recognizes high-signal cases including recursive call cycles,
unbounded loops, resource growth inside those loops, functions over 60 lines,
low defensive-check density, mutable defaults, broad swallowed exceptions,
dynamic execution, deep attribute chains, long parameter lists, unreachable
code, data clumps, duplicate function bodies, and several class-level smells.
Design-sensitive smells remain a semantic review responsibility.

## Install

Claude Code:

```sh
claude plugin marketplace add sciencejiho/reliable-python
claude plugin install reliable-python@jihohan-marketplace
```

Codex:

```sh
codex plugin marketplace add sciencejiho/reliable-python
codex plugin add reliable-python@jihohan-marketplace
```

Start a new session after installing, then review and trust the plugin's command
hooks when prompted.

## Layout

```text
.claude-plugin/
  plugin.json               # Claude Code manifest
  marketplace.json          # local marketplace (also legacy-compatible in Codex)
.codex-plugin/
  plugin.json               # Codex manifest
hooks/
  hooks.json                # shared SessionStart and Stop hooks
  session_start.py
  stop_quality_gate.py
skills/
  using-power-of-ten/       # entry policy and detailed references
  bounded-loops/            # focused Rule 2 workflow
  review-code-quality/      # audit workflow and static checker
tests/
  test_audit_python.py
```

## Test during development

Claude Code can load the checkout directly:

```sh
claude --plugin-dir /absolute/path/to/reliable-python
```

For Codex, add this repository as a local marketplace and install its entry:

```sh
codex plugin marketplace add /absolute/path/to/reliable-python
codex plugin add reliable-python@jihohan-marketplace
```

Start a new session after installing. Both hosts require you to review and trust
the plugin's command hooks before they run. In Codex, use `/hooks` to inspect
them. In Claude Code, use `/reload-plugins` after changing a directly loaded
development plugin.

## Run the checker directly

Audit only changed Python lines in the current Git working tree:

```sh
python3 skills/review-code-quality/scripts/audit_python.py --git-diff
```

Or audit explicit files/directories:

```sh
python3 skills/review-code-quality/scripts/audit_python.py src tests
```

The default CLI exit status fails on warnings or errors. Machine-readable output
and a different threshold are available with `--format json` and
`--fail-on error|warning|none`.

The Stop gate defaults to all findings. Set `RELIABLE_PYTHON_GATE=errors` before
launching the host to continue only on errors, or `RELIABLE_PYTHON_GATE=off` to
disable the completion gate while keeping skills and session guidance active.

For a confirmed false positive, use one adjacent, reasoned suppression:

```python
# quality: ignore[POT02] - top-level scheduler is intentionally non-terminating
while True:
    run_one_bounded_cycle()
```

## Validate

```sh
python3 -m unittest discover -s tests -v
claude plugin validate .
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```

## Contributing

This repository uses Git Flow and unscoped Conventional Commits. See
[CONTRIBUTING.md](CONTRIBUTING.md) for branch roles, release tagging, and commit
examples.
