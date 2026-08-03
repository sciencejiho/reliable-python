from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
AUDITOR_PATH = ROOT / "skills" / "review-code-quality" / "scripts" / "audit_python.py"
SPEC = importlib.util.spec_from_file_location("audit_python", AUDITOR_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDITOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDITOR
SPEC.loader.exec_module(AUDITOR)


def codes(source: str) -> set[str]:
    return {item.code for item in AUDITOR.analyze_source(source, pathlib.Path("sample.py"))}


class AuditPythonTests(unittest.TestCase):
    def test_detects_direct_and_indirect_recursion(self) -> None:
        source = """
def direct(value):
    if value <= 0:
        return 0
    return direct(value - 1)

def left(value):
    return right(value)

def right(value):
    return left(value)
"""
        findings = AUDITOR.analyze_source(source, pathlib.Path("recursive.py"))
        recursive_names = {item.message for item in findings if item.code == "POT01"}
        self.assertEqual(len(recursive_names), 3)

    def test_distinguishes_visible_and_unbounded_loops(self) -> None:
        bounded = """
MAX_ITEMS = 10

def consume():
    for index in range(MAX_ITEMS):
        if index < 0:
            raise ValueError(index)
        if index >= MAX_ITEMS:
            raise RuntimeError(index)
"""
        unbounded = """
def poll(client):
    while True:
        client.poll()
"""
        self.assertNotIn("POT02", codes(bounded))
        self.assertIn("POT02", codes(unbounded))

    def test_accepts_monotonic_while_with_named_bound(self) -> None:
        source = """
MAX_ATTEMPTS = 4

def retry():
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
"""
        self.assertNotIn("POT02", codes(source))

    def test_capped_slice_warns_about_silent_truncation(self) -> None:
        source = """
MAX_ITEMS = 10

def consume(items):
    for item in items[:MAX_ITEMS]:
        process(item)
"""
        findings = AUDITOR.analyze_source(source, pathlib.Path("capped.py"))
        messages = [item.message for item in findings if item.code == "POT02"]
        self.assertTrue(any("silently discard" in message for message in messages))

    def test_detects_dynamic_code_mutable_default_and_swallowed_error(self) -> None:
        source = """
def unsafe(items=[]):
    try:
        eval("1 + 1")
    except:
        pass
"""
        self.assertTrue({"POT06", "POT07", "POT08"}.issubset(codes(source)))

    def test_suppression_requires_and_honors_a_rationale(self) -> None:
        valid = """
def serve():
    # quality: ignore[POT02] - top-level service loop is intentionally non-terminating
    while True:
        tick()
"""
        malformed = """
def serve():
    # quality: ignore[POT02]
    while True:
        tick()
"""
        self.assertNotIn("POT02", codes(valid))
        self.assertIn("QLT001", codes(malformed))

    def test_detects_long_parameter_list_dead_code_and_message_chain(self) -> None:
        source = """
def transform(a, b, c, d, e, f):
    return a.b.c.d
    print("never")
"""
        self.assertTrue({"CS04", "CS16", "POT09"}.issubset(codes(source)))

    def test_filters_findings_to_changed_function_ranges(self) -> None:
        source = """
def old():
    while True:
        tick()

def new():
    eval("1")
"""
        findings = AUDITOR.analyze_source(source, pathlib.Path("/tmp/example.py"))
        filtered = AUDITOR.filter_changed_findings(
            findings,
            {pathlib.Path("/tmp/example.py"): ((6, 7),)},
        )
        self.assertIn("POT08", {item.code for item in filtered})
        self.assertNotIn("POT02", {item.code for item in filtered})


class HookIntegrationTests(unittest.TestCase):
    def test_session_start_emits_shared_context(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "session_start.py")],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("RELIABLE_PYTHON_POLICY", context)
        self.assertIn("code-smell", context)

    def test_stop_hook_blocks_a_new_changed_finding_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            target = root / "sample.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
            target.write_text("value = eval('1')\n", encoding="utf-8")
            hook_input = json.dumps({"cwd": str(root), "stop_hook_active": False})
            result = subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "stop_quality_gate.py")],
                input=hook_input,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["decision"], "block")
            self.assertIn("POT08", payload["reason"])


class PackageConsistencyTests(unittest.TestCase):
    def test_host_manifests_and_marketplace_versions_match(self) -> None:
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        entry = marketplace["plugins"][0]
        self.assertEqual({claude["name"], codex["name"], entry["name"]}, {"reliable-python"})
        self.assertEqual({claude["version"], codex["version"], entry["version"]}, {"0.2.0"})

    def test_session_policy_is_reinjected_after_every_start_mode(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        matcher = hooks["hooks"]["SessionStart"][0]["matcher"]
        self.assertEqual(set(matcher.split("|")), {"startup", "resume", "clear", "compact"})


if __name__ == "__main__":
    unittest.main()
