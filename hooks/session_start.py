#!/usr/bin/env python3
"""Inject the shared reliable-Python policy into Claude Code and Codex.

Both hosts run this at session start and add its output to the agent's context.
Skill descriptions are always visible, but nothing guarantees that an agent
reads a skill body. Injecting the compact entry skill makes the policy present
whenever the user has trusted and enabled the hook.

Fails open: if anything goes wrong we print nothing and exit 0, because a
broken hook must never block the user's session.
"""

import json
import pathlib
import subprocess
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY_SKILL = PLUGIN_ROOT / "skills" / "using-power-of-ten" / "SKILL.md"
AUDITOR = PLUGIN_ROOT / "skills" / "review-code-quality" / "scripts" / "audit_python.py"
MAX_RESOLVE_SECONDS = 10

# The entry skill is injected into EVERY session, so it must stay small.
# Tripping this ceiling means the skill has grown past its budget.
MAX_ENTRY_SKILL_BYTES = 8_192

PREAMBLE = (
    "<RELIABLE_PYTHON_POLICY>\n"
    "Apply this policy to Python code written or changed in this session. "
    "It combines a Python profile of the Power of Ten with code-smell review.\n\n"
    "The full `using-power-of-ten` entry skill follows. Invoke the bundled "
    "skills when their workflows apply.\n\n"
)


# quality: ignore[POT05] - this fail-open hook guards by returning sentinels, not raising
def _resolved_convention() -> str:
    """Ask the auditor which docstring convention this project actually uses.

    The auditor owns the precedence rules, so shelling out keeps one source of
    truth instead of duplicating resolution here.

    Returns
    -------
    str
        A Markdown line naming the active convention and where it came from, or
        an empty string when resolution fails for any reason.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(AUDITOR), "--print-docstring-style", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=MAX_RESOLVE_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    label = payload.get("label")
    source = payload.get("source")
    if not isinstance(label, str) or not isinstance(source, str):
        return ""
    return (
        f"\n\nActive documentation convention for this project: **{label}** "
        f"(source: {source})."
    )


def main() -> int:
    """Print the SessionStart payload carrying the shared policy text.

    Returns
    -------
    int
        Always ``0``. A hook that cannot read or size the entry skill reports
        the problem on stderr and still exits successfully, because a broken
        hook must never block the user's session.
    """
    try:
        body = ENTRY_SKILL.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"reliable-python: cannot read {ENTRY_SKILL}: {exc}",
              file=sys.stderr)
        return 0

    if len(body.encode("utf-8")) > MAX_ENTRY_SKILL_BYTES:
        print(
            f"reliable-python: entry skill exceeds "
            f"{MAX_ENTRY_SKILL_BYTES} bytes; move detail into a rule skill",
            file=sys.stderr,
        )
        return 0

    context = PREAMBLE + body + _resolved_convention() + "\n</RELIABLE_PYTHON_POLICY>"

    # Claude Code and Codex both read this SessionStart output shape.
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
