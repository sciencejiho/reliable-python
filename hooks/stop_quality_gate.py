#!/usr/bin/env python3
"""Run the changed-line quality audit once before an agent stops.

Claude Code and Codex share the Stop hook decision shape used here. The hook
fails open on infrastructure errors and allows a second stop to prevent an
infinite continuation loop.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDITOR = PLUGIN_ROOT / "skills" / "review-code-quality" / "scripts" / "audit_python.py"
MAX_REPORTED_FINDINGS = 12
VALID_GATE_LEVELS = {"all", "errors", "off"}


def _read_input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _emit(value: dict[str, Any]) -> int:
    json.dump(value, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_audit(cwd: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(AUDITOR),
                "--git-diff",
                "--format",
                "json",
                "--fail-on",
                "none",
            ],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"auditor exited {result.returncode}"
        return None, detail
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "auditor returned invalid JSON"
    return (payload, None) if isinstance(payload, dict) else (None, "auditor returned a non-object")


def _selected_findings(payload: dict[str, Any], gate_level: str) -> list[dict[str, Any]]:
    raw = payload.get("findings", [])
    findings = [item for item in raw if isinstance(item, dict)]
    if gate_level == "errors":
        return [item for item in findings if item.get("severity") == "error"]
    return findings


def _reason(findings: list[dict[str, Any]]) -> str:
    lines = [
        "The Reliable Python gate found changed Python that needs another pass.",
        "Fix confirmed findings, or add a narrow '# quality: ignore[ID] - rationale' only for a proven false positive, then rerun the audit.",
    ]
    for finding in findings[:MAX_REPORTED_FINDINGS]:
        path = finding.get("path", "<unknown>")
        line = finding.get("line", 1)
        severity = finding.get("severity", "warning")
        code = finding.get("code", "QUALITY")
        message = finding.get("message", "quality finding")
        lines.append(f"- {path}:{line}: {severity} {code}: {message}")
    remainder = len(findings) - MAX_REPORTED_FINDINGS
    if remainder > 0:
        lines.append(f"- ... and {remainder} more finding(s)")
    lines.append(f"Run: python3 {AUDITOR} --git-diff")
    return "\n".join(lines)


def main() -> int:
    hook_input = _read_input()
    gate_level = os.environ.get("RELIABLE_PYTHON_GATE", "all").lower()
    if gate_level not in VALID_GATE_LEVELS:
        return _emit(
            {
                "systemMessage": (
                    "Ignoring invalid "
                    f"RELIABLE_PYTHON_GATE={gate_level!r}."
                )
            }
        )
    if gate_level == "off":
        return _emit({})
    cwd = pathlib.Path(str(hook_input.get("cwd", pathlib.Path.cwd())))
    payload, error = _run_audit(cwd)
    if error or payload is None:
        return _emit({"systemMessage": f"Reliable-code gate skipped: {error}"})
    findings = _selected_findings(payload, gate_level)
    if not findings:
        return _emit({})
    reason = _reason(findings)
    if hook_input.get("stop_hook_active"):
        return _emit(
            {
                "systemMessage": (
                    "Reliable-code findings remain after one continuation; "
                    "allowing the turn to stop to avoid a hook loop.\n" + reason
                )
            }
        )
    return _emit({"decision": "block", "reason": reason})


if __name__ == "__main__":
    raise SystemExit(main())
