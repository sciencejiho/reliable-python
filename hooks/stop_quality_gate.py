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
from typing import Any, Sequence


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDITOR = PLUGIN_ROOT / "skills" / "review-code-quality" / "scripts" / "audit_python.py"
MAX_REPORTED_FINDINGS = 12
VALID_GATE_LEVELS = frozenset({"all", "errors", "off"})
VALID_DOCSTRING_STYLES = frozenset({"numpy", "google", "rest", "any"})
DEFAULT_DOCSTRING_STYLE = "numpy"
DOCSTYLE_ENV_VAR = "RELIABLE_PYTHON_DOCSTYLE"


# quality: ignore[POT05] - module checks JSON, enum, process, timeout, and payload boundaries
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


def _style_arguments() -> list[str]:
    """Pin the style only to override an invalid environment variable.

    Passing nothing lets the auditor resolve the convention itself, so a
    project's ``pyproject.toml`` setting is honored instead of being masked by
    an explicit flag.
    """
    requested = os.environ.get(DOCSTYLE_ENV_VAR)
    if requested is not None and requested.lower() not in VALID_DOCSTRING_STYLES:
        return ["--docstring-style", DEFAULT_DOCSTRING_STYLE]
    return []


def _style_notice() -> str:
    requested = os.environ.get(DOCSTYLE_ENV_VAR)
    if requested is None or requested.lower() in VALID_DOCSTRING_STYLES:
        return ""
    return (
        f"Ignoring invalid {DOCSTYLE_ENV_VAR}={requested!r}; "
        f"auditing as {DEFAULT_DOCSTRING_STYLE}."
    )


def _with_notice(payload: dict[str, Any], notice: str) -> dict[str, Any]:
    if not notice:
        return payload
    existing = payload.get("systemMessage", "")
    return {**payload, "systemMessage": f"{notice}\n{existing}" if existing else notice}


def _run_audit(
    cwd: pathlib.Path, style_arguments: Sequence[str]
) -> tuple[dict[str, Any] | None, str | None]:
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
                *style_arguments,
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
        "Fix confirmed findings, or add a narrow "
        "'# quality: ignore[ID] - rationale' only for a proven false positive, "
        "then rerun the audit.",
    ]
    # quality: ignore[POT02] - the cap is paired with an explicit remainder count below
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
    """Audit the changed Python scope and decide whether the turn may stop.

    Reads the host's Stop payload on stdin and writes the decision on stdout.
    `RELIABLE_PYTHON_GATE` selects which severities block, and
    `RELIABLE_PYTHON_DOCSTYLE` selects the expected docstring convention.

    Returns
    -------
    int
        Always ``0``. Infrastructure failures fail open with an explanatory
        message rather than blocking the turn, and a second consecutive stop is
        always allowed so the hook cannot loop.
    """
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
    notice = _style_notice()
    cwd = pathlib.Path(str(hook_input.get("cwd", pathlib.Path.cwd())))
    payload, error = _run_audit(cwd, _style_arguments())
    if error or payload is None:
        skipped = {"systemMessage": f"Reliable-code gate skipped: {error}"}
        return _emit(_with_notice(skipped, notice))
    findings = _selected_findings(payload, gate_level)
    if not findings:
        return _emit(_with_notice({}, notice))
    reason = _reason(findings)
    if hook_input.get("stop_hook_active"):
        exhausted = {
            "systemMessage": (
                "Reliable-code findings remain after one continuation; "
                "allowing the turn to stop to avoid a hook loop.\n" + reason
            )
        }
        return _emit(_with_notice(exhausted, notice))
    return _emit(_with_notice({"decision": "block", "reason": reason}, notice))


if __name__ == "__main__":
    raise SystemExit(main())
