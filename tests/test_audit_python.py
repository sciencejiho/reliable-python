from __future__ import annotations

import ast
import importlib.util
import itertools
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
AUDITOR_PATH = ROOT / "skills" / "review-code-quality" / "scripts" / "audit_python.py"
SPEC = importlib.util.spec_from_file_location("audit_python", AUDITOR_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDITOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDITOR
SPEC.loader.exec_module(AUDITOR)


# quality: ignore[POT05] - this trivial test projection has no boundary invariant
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

    def test_reports_every_member_of_a_branching_recursion_cycle(self) -> None:
        source = """
def origin():
    branch_left()
    branch_right()
    path_one()

def branch_left():
    leaf_one()
    leaf_two()
    leaf_three()

def branch_right():
    leaf_one()
    leaf_two()
    leaf_three()

def leaf_one():
    pass

def leaf_two():
    pass

def leaf_three():
    pass

def path_one():
    path_two()

def path_two():
    origin()
"""
        findings = AUDITOR.analyze_source(source, pathlib.Path("branching_cycle.py"))
        messages = {item.message for item in findings if item.code == "POT01"}
        self.assertEqual(
            messages,
            {
                "recursive call cycle includes origin",
                "recursive call cycle includes path_one",
                "recursive call cycle includes path_two",
            },
        )

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

    def test_rejects_conditional_or_reset_while_progress(self) -> None:
        conditional = """
MAX_ITEMS = 10

def consume(should_advance):
    index = 0
    while index < MAX_ITEMS:
        if should_advance:
            index += 1
        process(index)
"""
        reset = """
MAX_ITEMS = 10

def consume():
    index = 0
    while index < MAX_ITEMS:
        index += 1
        index = 0
"""
        self.assertIn("POT02", codes(conditional))
        self.assertIn("POT02", codes(reset))

    def test_range_and_islice_use_the_stop_argument_as_the_bound(self) -> None:
        unsafe = """
def consume(items, runtime_limit):
    for index in range(0, runtime_limit, 1):
        process(index)
    for item in itertools.islice(items, 0, runtime_limit, 1):
        process(item)
"""
        safe = """
MAX_ITEMS = 10

def consume(items):
    for index in range(0, MAX_ITEMS, 1):
        process(index)
    for item in itertools.islice(items, 0, MAX_ITEMS, 1):
        process(item)
"""
        unsafe_findings = AUDITOR.analyze_source(unsafe, pathlib.Path("unsafe_ranges.py"))
        unsafe_lines = {item.line for item in unsafe_findings if item.code == "POT02"}
        self.assertEqual(unsafe_lines, {3, 5})
        safe_findings = AUDITOR.analyze_source(safe, pathlib.Path("safe_ranges.py"))
        safe_messages = [item.message for item in safe_findings if item.code == "POT02"]
        self.assertFalse(any("depends on runtime data" in item for item in safe_messages))
        self.assertTrue(any("silently discard" in item for item in safe_messages))

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

    def test_malformed_suppression_findings_respect_the_finding_cap(self) -> None:
        source = "# quality: ignore[\n" * 3
        with mock.patch.object(AUDITOR, "MAX_FINDINGS", 2):
            findings = AUDITOR.analyze_source(source, pathlib.Path("suppressions.py"))
        self.assertEqual(len(findings), 2)
        self.assertEqual({item.code for item in findings}, {"LIMIT001", "QLT001"})

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
        io_error = AUDITOR.Finding(
            "IO001",
            "error",
            "/tmp/example.py",
            1,
            1,
            "cannot read source",
            "make it readable",
        )
        infrastructure = AUDITOR.filter_changed_findings(
            [io_error],
            {pathlib.Path("/tmp/example.py"): ()},
        )
        self.assertEqual(infrastructure, [io_error])

    def test_parent_map_does_not_monkey_patch_ast_nodes(self) -> None:
        tree = ast.parse("if ready:\n    run()\n")
        # quality: ignore[CS20] - regression test intentionally exercises the private seam
        parents = AUDITOR._build_parent_map(tree)
        child = tree.body[0]
        self.assertIs(parents[child], tree)
        self.assertNotIn("_quality_parent", vars(child))

    def test_oversized_source_returns_an_explicit_limit_error(self) -> None:
        source = "#" * (AUDITOR.MAX_SOURCE_BYTES + 1)
        self.assertIn("LIMIT001", codes(source))

    def test_non_utf8_source_text_returns_an_explicit_io_error(self) -> None:
        self.assertIn("IO001", codes("# \ud800"))

    def test_rejected_read_counts_against_the_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "sample.py"
            target.write_bytes(b"abcdef")
            # quality: ignore[CS20] - regression test intentionally exercises the private seam
            findings, bytes_read = AUDITOR._analyze_path(target, 3)
        self.assertEqual(bytes_read, 4)
        self.assertIn("LIMIT001", {item.code for item in findings})

    def test_failed_git_command_is_not_treated_as_empty_output(self) -> None:
        result = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=2,
            stdout=b"",
            stderr=b"broken repository",
        )
        with self.assertRaisesRegex(AUDITOR.AuditError, "broken repository"):
            # quality: ignore[CS20] - regression test intentionally exercises the private seam
            AUDITOR._git_stdout(result, "diff")

    def test_git_command_timeout_is_an_explicit_audit_error(self) -> None:
        timeout = subprocess.TimeoutExpired(["git", "diff"], AUDITOR.MAX_GIT_SECONDS)
        with mock.patch.object(AUDITOR.subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(AUDITOR.AuditError, "exceeded"):
                # quality: ignore[CS20] - regression test exercises the process boundary
                AUDITOR._run_git(pathlib.Path.cwd(), ["diff"])

    def test_input_iterators_are_capped_before_full_consumption(self) -> None:
        paths = itertools.repeat(pathlib.Path("unused.py"))
        with self.assertRaisesRegex(AUDITOR.AuditError, "input paths"):
            # quality: ignore[CS20] - regression test exercises bounded discovery
            tuple(AUDITOR._iter_python_files(paths))

    def test_discovery_cap_counts_non_python_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index in range(3):
                (root / f"entry-{index}.txt").write_text("data", encoding="utf-8")
            with mock.patch.object(AUDITOR, "MAX_DISCOVERED_PATHS", 2):
                with self.assertRaisesRegex(AUDITOR.AuditError, "discovered path"):
                    # quality: ignore[CS20] - regression test exercises bounded discovery
                    tuple(AUDITOR._iter_python_files([root]))


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
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
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

    def test_deletion_only_diff_is_attributed_to_the_enclosing_loop(self) -> None:
        baseline = """MAX_ITEMS = 10

def consume():
    index = 0
    while index < MAX_ITEMS:
        process(index)
        index += 1
"""
        modified = """MAX_ITEMS = 10

def consume():
    index = 0
    while index < MAX_ITEMS:
        process(index)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            target = root / "sample.py"
            target.write_text(baseline, encoding="utf-8")
            subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
            target.write_text(modified, encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(AUDITOR_PATH),
                    "--git-diff",
                    "--format",
                    "json",
                    "--fail-on",
                    "none",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            findings = json.loads(result.stdout)["findings"]
            self.assertIn("POT02", {item["code"] for item in findings})

    def test_missing_explicit_path_fails_instead_of_reporting_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "missing.py"
            result = subprocess.run(
                [sys.executable, str(AUDITOR_PATH), str(missing)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not exist", result.stderr)


class PackageConsistencyTests(unittest.TestCase):
    def test_host_manifests_and_marketplace_versions_match(self) -> None:
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        entry = marketplace["plugins"][0]
        self.assertEqual(
            {claude["name"], codex["name"], entry["name"]},
            {"reliable-python"},
        )
        self.assertEqual(
            {claude["version"], codex["version"], entry["version"]},
            {"0.2.1"},
        )

    def test_session_policy_is_reinjected_after_every_start_mode(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        matcher = hooks["hooks"]["SessionStart"][0]["matcher"]
        self.assertEqual(set(matcher.split("|")), {"startup", "resume", "clear", "compact"})


if __name__ == "__main__":
    unittest.main()
