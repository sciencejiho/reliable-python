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
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY_SKILL = PLUGIN_ROOT / "skills" / "using-power-of-ten" / "SKILL.md"

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


# quality: ignore[POT05] - this hook checks file I/O and the injected byte ceiling
def main() -> int:
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

    context = PREAMBLE + body + "\n</RELIABLE_PYTHON_POLICY>"

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
