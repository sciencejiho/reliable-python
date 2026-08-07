# Reliable Python

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.2.1-informational.svg)](.claude-plugin/plugin.json)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](skills/review-code-quality/scripts/audit_python.py)

A shared Codex and Claude Code plugin that holds your coding agent to strict
Python reliability rules — and checks whether it actually followed them.

It combines three bodies of guidance:

- Gerard Holzmann's [Power of Ten](https://spinroot.com/gerard/pdf/P10.pdf)
  rules, translated into an explicitly labeled Python profile
- the 23 smells in the
  [Refactoring.Guru catalog](https://refactoring.guru/ko/refactoring/smells)
- a documentation convention defaulting to
  [NumPy-style docstrings](https://numpydoc.readthedocs.io/en/latest/format.html)

> **What this is not.** The original rules target C and safety-critical
> development. This plugin does not certify Python code, prove safety, or claim
> to be an official NASA standard. Rules 3, 8, and 9 have no literal Python
> analogue and are labeled substitutions, not equivalents. What the plugin does
> is make the rules hard to overlook and mechanically check the subset that can
> be detected with useful confidence.

## Quickstart

**Claude Code**

```sh
claude plugin marketplace add sciencejiho/reliable-python
claude plugin install reliable-python@jihohan-marketplace
```

**Codex**

```sh
codex plugin marketplace add sciencejiho/reliable-python
codex plugin add reliable-python@jihohan-marketplace
```

Start a new session after installing, then review and trust the plugin's command
hooks when prompted. In Codex, inspect them with `/hooks`.

## What it looks like

Your agent writes a polling loop. It looks fine:

```python
def wait_for_job(api, job_id):
    while True:
        if api.get_status(job_id) == "done":
            return api.fetch_result(job_id)
        time.sleep(5)
```

Before the turn ends, the completion gate audits the changed lines:

```text
demo.py:6: warning DOC01: public function wait_for_job has no docstring
  remedy: Add a NumPy-style docstring, or make the name private if it is not part of the public surface.
demo.py:6: warning POT05: defensive-check density is 0.00 per function (target average: 2.00)
  remedy: Add meaningful boundary checks or document why trivial functions need none.
demo.py:7: error POT02: while loop has no mechanically visible preset upper bound
  remedy: Use a named maximum and fail explicitly when it is exhausted.
quality audit: 1 error(s), 2 warning(s)
```

The agent doesn't get to stop there. It gets one more pass:

```python
POLL_INTERVAL_S = 5
MAX_POLLS = 120  # 120 x 5s = 10 minute ceiling


def wait_for_job(api, job_id):
    """Block until a job finishes, or fail at a known time.

    Parameters
    ----------
    api : BatchClient
        Client for the batch service.
    job_id : str
        Identifier returned when the job was submitted.

    Returns
    -------
    Result
        The completed job's result.

    Raises
    ------
    ValueError
        If the client or job identifier is missing.
    TimeoutError
        If the job is still running after the poll ceiling.
    """
    if api is None:
        raise ValueError("api client is required")
    if not job_id:
        raise ValueError("job_id must be a non-empty identifier")
    for _ in range(MAX_POLLS):
        if api.get_status(job_id) == "done":
            return api.fetch_result(job_id)
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(
        f"job {job_id} still not done after "
        f"{MAX_POLLS * POLL_INTERVAL_S}s ({MAX_POLLS} polls)"
    )
```

That version audits clean. Three things changed, one per finding: the ceiling is
named and computable so a reader knows the worst case without arithmetic, the
guards satisfy the assertion-density rule at the boundary where inputs are
untrusted, and the error message says which job and how long.

The bound doesn't make the code correct. It makes the failure loud and fast
instead of silent and infinite.

## How it works

The same `skills/` and `hooks/` directories are loaded by both hosts.

1. A `SessionStart` hook injects the compact reliable-Python policy at startup,
   resume, clear, and compaction — so the rules are in context whether or not
   the agent chooses to read a skill.
2. The `using-power-of-ten` skill guides implementation continuously.
3. The `review-code-quality` skill performs an evidence-based semantic review
   across all ten rules and all five smell families.
4. Its dependency-free AST checker audits changed Python lines.
5. A `Stop` hook asks the agent for one more pass when the checker reports a
   changed-scope finding. It then allows a second stop to prevent a hook loop.

Only changed lines are reported, so adopting the plugin mid-project does not
bury you in findings about code you did not touch.

## What's inside

### Skills

| Skill | Loaded | Purpose |
|---|---|---|
| **using-power-of-ten** | Every session | Entry policy: the ten rules, the smell catalog, and the workflow that ties them together |
| **bounded-loops** | On demand | Rule 2 in depth — retries, polling, pagination, convergence, stream consumption |
| **limiting-nesting** | On demand | Rule 1 in depth — keeping block depth at four levels or fewer |
| **writing-docstrings** | On demand | NumPy-default documentation convention, with Google and reST as alternates |
| **review-code-quality** | On demand | Structured audit workflow and the bundled static checker |

The entry skill is injected into every session, so it is kept under a hard
8 KB ceiling. Detail lives in the on-demand skills and in
`skills/using-power-of-ten/references/`.

### Rules

| Namespace | Covers |
|---|---|
| `POT01`–`POT10` | Power of Ten, Python profile — see [references/power-of-ten.md](skills/using-power-of-ten/references/power-of-ten.md) |
| `CS01`–`CS23` | Refactoring.Guru smells in catalog order — see [references/code-smells.md](skills/using-power-of-ten/references/code-smells.md); `CS01` also covers [nesting depth](#nesting-depth) |
| `DOC01`–`DOC03` | Docstring convention — see [Docstrings](#docstrings) |

The checker recognizes high-signal cases including recursive call cycles,
unbounded loops, resource growth inside those loops, functions over 60 lines,
low defensive-check density, mutable defaults, broad swallowed exceptions,
dynamic execution, deep attribute chains, long parameter lists, unreachable
code, data clumps, duplicate function bodies, several class-level smells,
blocks nested more than four levels deep, and undocumented or inconsistently
documented public definitions.

Design-sensitive smells — divergent change, shotgun surgery, speculative
generality — stay a semantic review responsibility. No static checker can
confirm them, and the skill is explicit that a smell needs a demonstrated cost
before it becomes a finding.

## Nesting depth

A function may nest blocks four levels deep; the function body is level one.
Past that, the checker reports `CS01` (Long Method), whose catalog signal
explicitly includes deep nesting, anchored at the deepest statement rather than
the definition:

```text
CS01: function collect_errors nests 5 levels deep (limit: 4)
  remedy: Extract the inner block into a named function, or invert a condition
          to return early and remove a level.
```

An `if`/`elif` ladder counts as one level, not one per branch — it reads as a
single flat decision. `POT04` covers the other half of `CS01`: a function over
60 code lines. The `limiting-nesting` skill covers the techniques.

## Docstrings

Public modules, classes, and functions default to NumPy style, which keeps a
parameter's type, default, and constraints together instead of compressing them
onto one line. Google and reST style are accepted for files that already use
them; the `writing-docstrings` skill covers the templates and the choice.

| ID | Fires when | numpydoc equivalent |
|---|---|---|
| `DOC01` | A public module, class, or function has no docstring | `GL08` |
| `DOC02` | The sections identify a style other than the configured one | — |
| `DOC03` | A parameter section exists but documents only some parameters | `PR01` |

A summary-only docstring never triggers `DOC02`, because one line with no
sections is valid in all three conventions. `DOC01` skips private names,
dunders, nested functions, `@overload` and `...` stubs, property setters and
deleters, `__init__.py`, methods of private classes, and `test_*` functions in
test modules.

Because docstrings are required, the 60-line `POT04` limit measures code lines
and excludes the docstring span.

## Running the checker directly

Audit only changed Python lines in the current Git working tree:

```sh
python3 skills/review-code-quality/scripts/audit_python.py --git-diff
```

Or audit explicit files and directories:

```sh
python3 skills/review-code-quality/scripts/audit_python.py src tests
```

### Configuration

| Setting | Values | Default | Effect |
|---|---|---|---|
| `--format` | `text`, `json` | `text` | Machine-readable output for tooling |
| `--fail-on` | `none`, `error`, `warning` | `warning` | Exit-status threshold |
| `--docstring-style` | `numpy`, `google`, `rest`, `any` | resolved | Expected convention; `any` disables `DOC02` |
| `RELIABLE_PYTHON_GATE` | `all`, `errors`, `off` | `all` | Which severities block the completion gate |
| `RELIABLE_PYTHON_DOCSTYLE` | as `--docstring-style` | resolved | Convention the gate expects |

Set the environment variables before launching the host. `RELIABLE_PYTHON_GATE=off`
disables the completion gate while keeping skills and session guidance active.

### Choosing a docstring convention

Declare it once in the project's `pyproject.toml`, so every session, teammate,
and CI run agrees:

```toml
[tool.reliable-python]
docstring-style = "google"   # numpy (default), google, rest, or any
```

The nearest `pyproject.toml` at or above the working directory is used.
Precedence, highest first:

| Source | Scope |
|---|---|
| `--docstring-style` | One command |
| `RELIABLE_PYTHON_DOCSTYLE` | One shell session |
| `[tool.reliable-python] docstring-style` | The project, committed to the repository |
| built-in default (`numpy`) | Everything else |

The setting drives both enforcement and guidance: the session policy injected
into the agent names the resolved convention, so it never asks for NumPy while
the checker accepts Google. Ask what is in effect with:

```sh
python3 skills/review-code-quality/scripts/audit_python.py --print-docstring-style
```

```text
google (source: /path/to/project/pyproject.toml)
```

An unrecognized value is an explicit error rather than a silent fallback, except
in the completion gate, which reports it and audits as `numpy` so a typo cannot
quietly disable the check.

Reading `pyproject.toml` uses `tomllib` and therefore needs Python 3.11 or
newer. On 3.10 the flag and the environment variable still work, and the
reported source says so.

### Suppressions

For a confirmed false positive, use one adjacent, reasoned suppression:

```python
# quality: ignore[POT02] - top-level scheduler is intentionally non-terminating
while True:
    run_one_bounded_cycle()
```

The rationale is required — a bare marker is itself a `QLT001` error. A
suppression records a decision; it does not turn non-compliant code into
compliant code.

## Development

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
  limiting-nesting/         # focused Rule 1 nesting-depth workflow
  writing-docstrings/       # NumPy-default documentation convention
  review-code-quality/      # audit workflow and static checker
tests/
  test_audit_python.py
```

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
the plugin's command hooks before they run. Use `/reload-plugins` in Claude Code
after changing a directly loaded development plugin.

### Validate

```sh
python3 -m unittest discover -s tests -v
python3 skills/review-code-quality/scripts/audit_python.py hooks tests skills
claude plugin validate .
```

The repository is audited by its own checker and must stay at zero findings.
See [CLAUDE.md](CLAUDE.md) for the architecture and the constraints that
imposes on contributions.

## Contributing

This repository uses Git Flow and unscoped Conventional Commits. See
[CONTRIBUTING.md](CONTRIBUTING.md) for branch roles, release tagging, and commit
examples.

## License

MIT — see [LICENSE](LICENSE).
