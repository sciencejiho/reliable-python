"""Tests for the bundled quality auditor and the shared plugin hooks.

The auditor is loaded from its script path rather than imported as a package,
because it ships as a dependency-free file that both hosts execute directly.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import json
import os
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
    """Audit a source snippet and project the findings to their rule codes.

    Parameters
    ----------
    source : str
        Python source audited as ``sample.py`` with the NumPy default style.

    Returns
    -------
    set of str
        Distinct rule codes the auditor reported.
    """
    return {item.code for item in AUDITOR.analyze_source(source, pathlib.Path("sample.py"))}


class AuditPythonTests(unittest.TestCase):
    """Cover the Power of Ten and code-smell rules in the bundled auditor."""

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
            findings, bytes_read = AUDITOR._analyze_path(target, 3, "numpy")
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


class DocstringTests(unittest.TestCase):
    """Cover the DOC01-DOC03 documentation convention rules."""

    def test_flags_missing_docstrings_on_public_definitions_only(self) -> None:
        source = '''"""Module."""


def public(value):
    return value


def _private(value):
    return value


class Public:
    pass


class _Private:
    pass
'''
        findings = AUDITOR.analyze_source(source, pathlib.Path("sample.py"))
        subjects = [item.message for item in findings if item.code == "DOC01"]
        self.assertEqual(len(subjects), 2)
        self.assertTrue(any("function public" in item for item in subjects))
        self.assertTrue(any("class Public" in item for item in subjects))

    def test_exempts_nested_stub_dunder_and_decorated_definitions(self) -> None:
        source = '''"""Module."""

from typing import overload


class Holder:
    """Holder."""

    def __init__(self, value):
        self.value = value

    @property
    def item(self):
        """Return the item."""
        return self.value

    @item.setter
    def item(self, value):
        self.value = value


@overload
def widen(value: int) -> int: ...


def outer(value):
    """Wrap an inner helper.

    Parameters
    ----------
    value : int
        Value to wrap.
    """

    def inner(item):
        return item

    return inner(value)
'''
        self.assertNotIn("DOC01", codes(source))

    def test_methods_of_a_private_class_are_not_public_surface(self) -> None:
        source = '''"""Module."""


class _Private:
    """Private."""

    def public_method(self, value):
        return value
'''
        self.assertNotIn("DOC01", codes(source))

    def test_async_definitions_are_covered(self) -> None:
        source = '"""Module."""\n\n\nasync def fetch(value):\n    return value\n'
        self.assertIn("DOC01", codes(source))

    def test_exempts_test_functions_in_test_modules_only(self) -> None:
        source = '''"""Module."""


def test_behavior():
    assert True
'''
        in_test_module = {
            item.code
            for item in AUDITOR.analyze_source(source, pathlib.Path("test_sample.py"))
        }
        self.assertNotIn("DOC01", in_test_module)
        self.assertIn("DOC01", codes(source))

    def test_missing_module_docstring_is_reported_except_for_packages(self) -> None:
        source = "VALUE = 1\n"
        self.assertIn("DOC01", codes(source))
        package = {
            item.code
            for item in AUDITOR.analyze_source(source, pathlib.Path("__init__.py"))
        }
        self.assertNotIn("DOC01", package)

    def test_detects_each_supported_docstring_style(self) -> None:
        numpy_text = "Summary.\n\nParameters\n----------\nvalue : int\n    A value.\n"
        google_text = "Summary.\n\nArgs:\n    value (int): A value.\n"
        rest_text = "Summary.\n\n:param int value: A value.\n"
        self.assertEqual(AUDITOR.detect_docstring_style(numpy_text), "numpy")
        self.assertEqual(AUDITOR.detect_docstring_style(google_text), "google")
        self.assertEqual(AUDITOR.detect_docstring_style(rest_text), "rest")

    def test_summary_only_docstring_has_no_detectable_style(self) -> None:
        self.assertIsNone(AUDITOR.detect_docstring_style("Return the value."))
        source = '''"""Module."""


def public(value):
    """Return the value unchanged."""
    return value
'''
        self.assertNotIn("DOC02", codes(source))

    def test_reports_a_competing_style_against_the_configured_default(self) -> None:
        source = '''"""Module."""


def public(value):
    """Return the value.

    Args:
        value (int): A value.
    """
    return value
'''
        self.assertIn("DOC02", codes(source))
        as_google = {
            item.code
            for item in AUDITOR.analyze_source(
                source, pathlib.Path("sample.py"), "google"
            )
        }
        self.assertNotIn("DOC02", as_google)
        as_any = {
            item.code
            for item in AUDITOR.analyze_source(source, pathlib.Path("sample.py"), "any")
        }
        self.assertNotIn("DOC02", as_any)

    def test_unknown_docstring_style_is_rejected(self) -> None:
        with self.assertRaisesRegex(AUDITOR.AuditError, "unknown docstring style"):
            AUDITOR.analyze_source('"""M."""\n', pathlib.Path("sample.py"), "epytext")

    def test_partial_parameter_section_is_reported(self) -> None:
        source = '''"""Module."""


def public(alpha, beta, *rest, **options):
    """Combine values.

    Parameters
    ----------
    alpha : int
        First value.

    Returns
    -------
    int
        The combination.
    """
    return alpha + beta + len(rest) + len(options)
'''
        findings = [item for item in AUDITOR.analyze_source(source, pathlib.Path("sample.py"))
                    if item.code == "DOC03"]
        self.assertEqual(len(findings), 1)
        self.assertIn("beta", findings[0].message)
        self.assertIn("rest", findings[0].message)
        self.assertIn("options", findings[0].message)

    def test_complete_parameter_sections_are_accepted_in_every_style(self) -> None:
        numpy_source = '''"""Module."""


def public(alpha, beta):
    """Combine values.

    Parameters
    ----------
    alpha, beta : int
        Values to combine.
    """
    return alpha + beta
'''
        self.assertNotIn("DOC03", codes(numpy_source))
        google_source = '''"""Module."""


def public(alpha, beta):
    """Combine values.

    Args:
        alpha (int): First value.
        beta (int): Second value.
    """
    return alpha + beta
'''
        self.assertNotIn(
            "DOC03",
            {
                item.code
                for item in AUDITOR.analyze_source(
                    google_source, pathlib.Path("sample.py"), "google"
                )
            },
        )

    def test_no_parameter_section_does_not_trigger_coverage(self) -> None:
        source = '''"""Module."""


def public(alpha, beta):
    """Combine values.

    Returns
    -------
    int
        The combination.
    """
    return alpha + beta
'''
        self.assertNotIn("DOC03", codes(source))

    def test_oversized_docstring_reports_an_explicit_limit(self) -> None:
        body = "\n".join(str(index) for index in range(AUDITOR.MAX_DOCSTRING_LINES + 2))
        source = f'"""{body}"""\n'
        self.assertIn("LIMIT001", codes(source))

    def test_docstring_lines_do_not_count_against_the_function_limit(self) -> None:
        documentation = "\n".join(f"    line {index}." for index in range(40))
        source = (
            '"""Module."""\n\n\ndef public(value):\n'
            '    """Summary.\n\n'
            f"{documentation}\n"
            '    """\n'
            "    return value\n"
        )
        self.assertNotIn("POT04", codes(source))


class DocstringStyleResolutionTests(unittest.TestCase):
    """Cover precedence across the flag, the environment, and pyproject.toml."""

    def _project(self, directory: str, body: str) -> pathlib.Path:
        root = pathlib.Path(directory)
        (root / "pyproject.toml").write_text(body, encoding="utf-8")
        return root

    def test_flag_outranks_environment_and_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "rest"\n'
            )
            style, source = AUDITOR.resolve_docstring_style(
                "google", {"RELIABLE_PYTHON_DOCSTYLE": "numpy"}, root
            )
        self.assertEqual(style, "google")
        self.assertEqual(source, "--docstring-style")

    def test_environment_outranks_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "rest"\n'
            )
            style, source = AUDITOR.resolve_docstring_style(
                None, {"RELIABLE_PYTHON_DOCSTYLE": "google"}, root
            )
        self.assertEqual(style, "google")
        self.assertEqual(source, "RELIABLE_PYTHON_DOCSTYLE")

    def test_configuration_outranks_the_built_in_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "google"\n'
            )
            style, source = AUDITOR.resolve_docstring_style(None, {}, root)
        self.assertEqual(style, "google")
        self.assertIn("pyproject.toml", source)

    def test_configuration_is_found_in_a_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "google"\n'
            )
            nested = root / "src" / "deep"
            nested.mkdir(parents=True)
            style, _ = AUDITOR.resolve_docstring_style(None, {}, nested)
        self.assertEqual(style, "google")

    def test_missing_or_unrelated_configuration_falls_back_to_numpy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bare_style, bare_source = AUDITOR.resolve_docstring_style(None, {}, root)
            self.assertEqual((bare_style, bare_source), ("numpy", "built-in default"))
            self._project(directory, '[project]\nname = "sample"\n')
            style, source = AUDITOR.resolve_docstring_style(None, {}, root)
        self.assertEqual((style, source), ("numpy", "built-in default"))

    def test_unknown_style_is_rejected_with_its_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "epytext"\n'
            )
            with self.assertRaisesRegex(AUDITOR.AuditError, "epytext"):
                AUDITOR.resolve_docstring_style(None, {}, root)
        with self.assertRaisesRegex(AUDITOR.AuditError, "RELIABLE_PYTHON_DOCSTYLE"):
            AUDITOR.resolve_docstring_style(
                None, {"RELIABLE_PYTHON_DOCSTYLE": "epytext"}, pathlib.Path.cwd()
            )

    def test_malformed_configuration_is_an_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(directory, '[tool.reliable-python\nx = "y"\n')
            with self.assertRaisesRegex(AUDITOR.AuditError, "cannot parse"):
                AUDITOR.resolve_docstring_style(None, {}, root)

    def test_print_docstring_style_reports_the_resolved_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._project(
                directory, '[tool.reliable-python]\ndocstring-style = "google"\n'
            )
            result = subprocess.run(
                [sys.executable, str(AUDITOR_PATH), "--print-docstring-style",
                 "--format", "json"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                env={key: value for key, value in os.environ.items()
                     if key != "RELIABLE_PYTHON_DOCSTYLE"},
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["style"], "google")
        self.assertEqual(payload["label"], "Google")
        self.assertIn("pyproject.toml", payload["source"])


class HookIntegrationTests(unittest.TestCase):
    """Cover the SessionStart and Stop hook contracts."""

    def test_session_start_states_the_active_convention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "pyproject.toml").write_text(
                '[tool.reliable-python]\ndocstring-style = "google"\n', encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "session_start.py")],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                env={key: value for key, value in os.environ.items()
                     if key != "RELIABLE_PYTHON_DOCSTYLE"},
            )
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Active documentation convention", context)
        self.assertIn("**Google**", context)
        self.assertNotIn("**NumPy-style**", context)

    def test_stop_gate_honors_project_configuration(self) -> None:
        google_source = (
            '"""Module."""\n\n\ndef public(value):\n'
            '    """Return the value.\n\n'
            "    Args:\n"
            "        value (int): A value.\n"
            '    """\n'
            "    return value\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sample.py").write_text(google_source, encoding="utf-8")
            hook_input = json.dumps({"cwd": str(root), "stop_hook_active": False})
            blocked = self._run_stop_hook(hook_input, {})
            self.assertIn("DOC02", blocked["reason"])
            (root / "pyproject.toml").write_text(
                '[tool.reliable-python]\ndocstring-style = "google"\n', encoding="utf-8"
            )
            configured = self._run_stop_hook(hook_input, {})
            self.assertNotIn("DOC02", configured.get("reason", ""))

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

    def test_stop_hook_honors_the_docstring_style_environment_variable(self) -> None:
        google_source = (
            '"""Module."""\n\n\ndef public(value):\n'
            '    """Return the value.\n\n'
            "    Args:\n"
            "        value (int): A value.\n"
            '    """\n'
            "    return value\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sample.py").write_text(google_source, encoding="utf-8")
            hook_input = json.dumps({"cwd": str(root), "stop_hook_active": False})
            blocked = self._run_stop_hook(hook_input, {})
            self.assertEqual(blocked["decision"], "block")
            self.assertIn("DOC02", blocked["reason"])
            accepted = self._run_stop_hook(
                hook_input, {"RELIABLE_PYTHON_DOCSTYLE": "google"}
            )
            self.assertNotIn("DOC02", accepted.get("reason", ""))
            invalid = self._run_stop_hook(
                hook_input, {"RELIABLE_PYTHON_DOCSTYLE": "epytext"}
            )
            self.assertEqual(invalid["decision"], "block")
            self.assertIn("Ignoring invalid", invalid["systemMessage"])

    def _run_stop_hook(self, hook_input: str, environment: dict[str, str]) -> dict:
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "stop_quality_gate.py")],
            input=hook_input,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **environment},
        )
        return json.loads(result.stdout)

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
    """Keep the two host manifests and the local marketplace in agreement."""

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
