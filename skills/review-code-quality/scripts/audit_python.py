#!/usr/bin/env python3
"""Audit Python for high-signal Power of Ten and code-smell findings.

This checker is intentionally dependency-free. It catches mechanically visible
signals and leaves design-sensitive smells to the companion skill's semantic
review. It is a guardrail, not a safety proof or a full static analyzer.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Sequence

try:
    import tomllib
except ImportError:  # Python 3.10 has no tomllib; project config is unavailable.
    tomllib = None


EXCLUDED_DIRECTORIES = frozenset({
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
})
MAX_INPUT_PATHS = 256
MAX_PYTHON_FILES = 4_096
MAX_DISCOVERED_PATHS = 32_768
MAX_DIRECTORY_VISITS = MAX_DISCOVERED_PATHS + MAX_INPUT_PATHS + 1
MAX_SOURCE_BYTES = 2_000_000
MAX_TOTAL_SOURCE_BYTES = 32_000_000
MAX_GIT_OUTPUT_BYTES = 32_000_000
MAX_GIT_SECONDS = 30
MAX_FINDINGS = 10_000
MAX_AST_NODES = 100_000
MAX_CALL_GRAPH_STEPS = 1_000_000
MAX_CHAIN_DEPTH = 64
MAX_NUMERIC_UNARY_DEPTH = 8
MUTABLE_BUILDERS = frozenset({"dict", "list", "set", "defaultdict", "OrderedDict"})
DYNAMIC_BUILTINS = frozenset({"__import__", "compile", "eval", "exec"})
REFLECTION_BUILTINS = frozenset({"delattr", "getattr", "setattr"})
MUTATING_METHODS = frozenset({"add", "append", "extend", "insert", "setdefault", "update"})
TERMINATORS = (ast.Break, ast.Continue, ast.Raise, ast.Return)
MAX_DOCSTRING_LINES = 2_000
MAX_CONFIG_BYTES = 1_000_000
MAX_CONFIG_SEARCH_DEPTH = 64
CONFIG_FILENAME = "pyproject.toml"
CONFIG_TABLE = "reliable-python"
CONFIG_KEY = "docstring-style"
DOCSTYLE_ENV_VAR = "RELIABLE_PYTHON_DOCSTYLE"
DEFAULT_DOCSTRING_STYLE = "numpy"
DOCSTRING_STYLES = ("numpy", "google", "rest", "any")
STYLE_LABELS = MappingProxyType({"numpy": "NumPy", "google": "Google", "rest": "reST"})
# Canonical numpydoc sections; see https://numpydoc.readthedocs.io/en/latest/format.html
NUMPY_SECTION_NAMES = frozenset({
    "Attributes",
    "Examples",
    "Methods",
    "Notes",
    "Other Parameters",
    "Parameters",
    "Raises",
    "Receives",
    "References",
    "Returns",
    "See Also",
    "Warnings",
    "Warns",
    "Yields",
})
GOOGLE_SECTION_NAMES = frozenset({
    "Args",
    "Arguments",
    "Attributes",
    "Example",
    "Examples",
    "Keyword Args",
    "Keyword Arguments",
    "Methods",
    "Note",
    "Notes",
    "Other Parameters",
    "Raises",
    "References",
    "Returns",
    "See Also",
    "Todo",
    "Warning",
    "Warnings",
    "Yields",
})
GOOGLE_PARAMETER_SECTIONS = frozenset({
    "Args",
    "Arguments",
    "Keyword Args",
    "Keyword Arguments",
})
DOCSTRING_EXEMPT_DECORATORS = frozenset({"deleter", "overload", "setter"})
REST_FIELD_RE = re.compile(
    r"^\s*:(?:param|parameter|arg|argument|key|keyword|type|returns?|rtype"
    r"|raises?|except|exception|var|ivar|cvar|vartype)\b"
)
REST_PARAMETER_RE = re.compile(
    r"^\s*:(?:param|parameter|arg|argument|key|keyword)\s+"
    r"(?:[^\s:]+\s+)?(?P<name>\*{0,2}\w+)\s*:"
)
SUPPRESSION_TOKEN = "quality: ignore["
SUPPRESSION_RE = re.compile(
    r"#\s*quality:\s*ignore\[([A-Z][A-Z0-9]+)\]\s*-\s*(\S.*)$"
)
HUNK_RE = re.compile(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")
SEVERITY_RANK = MappingProxyType({"note": 0, "warning": 1, "error": 2})


class AuditError(ValueError):
    """Raised when a requested audit cannot be completed reliably."""


@dataclass(frozen=True)
class Finding:
    """One reported rule violation or code smell.

    Attributes
    ----------
    code : str
        Rule identifier, such as ``"POT02"``, ``"CS14"``, or ``"DOC01"``.
    severity : str
        One of ``"note"``, ``"warning"``, or ``"error"``.
    path : str
        File the finding was reported against.
    line : int
        First source line covered by the finding, 1-indexed.
    end_line : int
        Last source line covered, used to intersect against a diff.
    message : str
        What the code demonstrably does.
    remedy : str
        Smallest correction that resolves the finding.
    """

    code: str
    severity: str
    path: str
    line: int
    end_line: int
    message: str
    remedy: str


# quality: ignore[POT05] - module boundaries validate CLI, Git, source, AST, and output data
def _limit_finding(path: Path, message: str, remedy: str) -> Finding:
    return Finding("LIMIT001", "error", str(path), 1, 1, message, remedy)


def _record_finding(findings: list[Finding], finding: Finding) -> None:
    if len(findings) >= MAX_FINDINGS:
        findings[MAX_FINDINGS - 1] = finding
    else:
        findings.append(finding)


def _record_limit(
    findings: list[Finding], path: Path, message: str, remedy: str
) -> None:
    _record_finding(findings, _limit_finding(path, message, remedy))


@dataclass(frozen=True)
class FunctionInfo:
    """A function or method located in the parsed module.

    Attributes
    ----------
    qualified_name : str
        Dotted path from module scope, such as ``"ReviewContext.report"``.
    class_name : str or None
        Dotted name of the enclosing class, or ``None`` for a plain function.
    node : ast.FunctionDef or ast.AsyncFunctionDef
        Definition node this record describes.
    """

    qualified_name: str
    class_name: str | None
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class DiffSelection:
    """The changed Python scope discovered from a Git working tree.

    Attributes
    ----------
    root : Path
        Absolute repository root the paths are resolved against.
    paths : tuple of Path
        Changed Python files that exist on disk.
    changed_lines : dict
        Maps each path to the inclusive line ranges that the diff touched.
    """

    root: Path
    paths: tuple[Path, ...]
    changed_lines: dict[Path, tuple[tuple[int, int], ...]]


class SourceLines(list[str]):
    """Source lines plus suppression comments identified by Python tokenization."""

    def __init__(self, source: str) -> None:
        super().__init__(source.splitlines())
        self.suppressions: dict[int, set[str]] = defaultdict(set)
        self.invalid_suppressions: set[int] = set()
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            # quality: ignore[POT02] - source is rejected above MAX_SOURCE_BYTES
            for token in tokens:
                if token.type != tokenize.COMMENT or SUPPRESSION_TOKEN not in token.string:
                    continue
                match = SUPPRESSION_RE.search(token.string)
                if match:
                    # quality: ignore[POT03] - markers are capped by source byte size
                    self.suppressions[token.start[0]].add(match.group(1))
                else:
                    # quality: ignore[POT03] - markers are capped by source byte size
                    self.invalid_suppressions.add(token.start[0])
        except (IndentationError, tokenize.TokenError):
            pass


@dataclass
class ReviewContext:
    """Per-file state shared by every check in one audit pass."""

    tree: ast.Module
    lines: SourceLines
    path: Path
    parents: dict[ast.AST, ast.AST]
    findings: list[Finding]
    docstring_style: str = DEFAULT_DOCSTRING_STYLE

    def report(
        self,
        *,
        code: str,
        severity: str,
        node: ast.AST,
        message: str,
        remedy: str,
    ) -> None:
        """Record one finding unless a suppression covers it.

        Parameters
        ----------
        code : str
            Rule identifier to report.
        severity : str
            One of ``"note"``, ``"warning"``, or ``"error"``.
        node : ast.AST
            Node whose line range the finding anchors to.
        message : str
            What the code demonstrably does.
        remedy : str
            Smallest correction that resolves the finding.

        Raises
        ------
        AuditError
            If the file already holds ``MAX_FINDINGS`` findings.
        """
        # quality: ignore[POT08] - ast.AST line metadata is optional by contract
        line = int(getattr(node, "lineno", 1))
        if _is_suppressed(self.lines, line, code):
            return
        if len(self.findings) >= MAX_FINDINGS:
            raise AuditError(f"finding count exceeds {MAX_FINDINGS}")
        self.findings.append(
            Finding(
                code,
                severity,
                str(self.path),
                line,
                _node_end(node),
                message,
                remedy,
            )
        )


def _run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *arguments],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=MAX_GIT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AuditError(
            f"git command exceeded {MAX_GIT_SECONDS} seconds"
        ) from error


def _git_stdout(result: subprocess.CompletedProcess[bytes], operation: str) -> bytes:
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise AuditError(f"git {operation} failed: {detail or f'exit {result.returncode}'}")
    if len(result.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise AuditError(
            f"git {operation} output exceeds {MAX_GIT_OUTPUT_BYTES} bytes"
        )
    return result.stdout


def _git_root(cwd: Path) -> Path:
    result = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise AuditError("--git-diff requires a Git working tree")
    stdout = _git_stdout(result, "rev-parse")
    return Path(stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()


def _has_head(root: Path) -> bool:
    return _run_git(root, ["rev-parse", "--verify", "HEAD"]).returncode == 0


def _decode_nul_paths(payload: bytes) -> set[str]:
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in payload.split(b"\0")
        if item
    }


def _changed_path_names(root: Path, has_head: bool) -> tuple[set[str], set[str]]:
    diff_args = ["diff", "--name-only", "-z", "--diff-filter=ACMR"]
    if has_head:
        result = _run_git(root, [*diff_args, "HEAD", "--", "*.py"])
        tracked = _decode_nul_paths(_git_stdout(result, "diff --name-only"))
    else:
        cached_result = _run_git(root, [*diff_args, "--cached", "--", "*.py"])
        unstaged_result = _run_git(root, [*diff_args, "--", "*.py"])
        cached = _decode_nul_paths(_git_stdout(cached_result, "diff --cached --name-only"))
        unstaged = _decode_nul_paths(_git_stdout(unstaged_result, "diff --name-only"))
        tracked = cached | unstaged
    untracked_result = _run_git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"],
    )
    untracked = _decode_nul_paths(_git_stdout(untracked_result, "ls-files --others"))
    return tracked, untracked


def _parse_patch_ranges(payload: bytes, root: Path) -> dict[Path, list[tuple[int, int]]]:
    ranges: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    current_path: Path | None = None
    text = payload.decode("utf-8", errors="replace")
    # quality: ignore[POT02] - payload is rejected above MAX_GIT_OUTPUT_BYTES
    for line in text.splitlines():
        if line.startswith("+++ "):
            name = line[4:]
            if name == "/dev/null":
                current_path = None
            else:
                current_path = (root / (name[2:] if name.startswith("b/") else name)).resolve()
            continue
        match = HUNK_RE.match(line)
        if current_path is None or match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count > 0:
            # quality: ignore[POT03] - one range per hunk in the bounded payload
            ranges[current_path].append((start, start + count - 1))
        else:
            # quality: ignore[POT03] - one range per hunk in the bounded payload
            ranges[current_path].append((max(1, start - 1), max(1, start)))
    return ranges


def _patch_ranges(root: Path, has_head: bool) -> dict[Path, list[tuple[int, int]]]:
    args = ["diff", "--unified=0", "--no-color"]
    payloads: list[bytes]
    if has_head:
        result = _run_git(root, [*args, "HEAD", "--", "*.py"])
        payloads = [_git_stdout(result, "diff patch")]
    else:
        cached_result = _run_git(root, [*args, "--cached", "--", "*.py"])
        unstaged_result = _run_git(root, [*args, "--", "*.py"])
        payloads = [
            _git_stdout(cached_result, "diff --cached patch"),
            _git_stdout(unstaged_result, "diff patch"),
        ]
    combined: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    # quality: ignore[POT02] - payloads contains at most cached and unstaged output
    for payload in payloads:
        # quality: ignore[POT02] - each parsed range set is capped by Git output bytes
        for path, items in _parse_patch_ranges(payload, root).items():
            # quality: ignore[POT03] - combined hunk count is capped by Git output bytes
            combined[path].extend(items)
    return combined


def collect_git_diff(cwd: Path) -> DiffSelection:
    """Determine which Python files and lines the working tree changed.

    Tracked changes are read against ``HEAD`` when it exists, and against the
    index otherwise. Untracked files count as changed in their entirety.

    Parameters
    ----------
    cwd : Path
        Directory inside the Git working tree to inspect.

    Returns
    -------
    DiffSelection
        Repository root, changed Python files, and their changed line ranges.

    Raises
    ------
    AuditError
        If `cwd` is outside a Git working tree, a Git command fails or times
        out, or the change set exceeds ``MAX_PYTHON_FILES``.
    """
    root = _git_root(cwd)
    has_head = _has_head(root)
    tracked, untracked = _changed_path_names(root, has_head)
    ranges = _patch_ranges(root, has_head)
    all_names = sorted(tracked | untracked)
    if len(all_names) > MAX_PYTHON_FILES:
        raise AuditError(
            f"changed Python file count exceeds {MAX_PYTHON_FILES}"
        )
    paths = tuple((root / name).resolve() for name in all_names if (root / name).is_file())
    # quality: ignore[POT02] - all_names is rejected above MAX_PYTHON_FILES
    for name in untracked:
        path = (root / name).resolve()
        if not path.is_file():
            continue
        ranges[path] = [(1, MAX_SOURCE_BYTES + 1)]
    frozen_ranges = {path: tuple(ranges.get(path, ())) for path in paths}
    return DiffSelection(root=root, paths=paths, changed_lines=frozen_ranges)


def _register_python_file(candidate: Path, seen: set[Path]) -> Path | None:
    resolved = candidate.resolve()
    if not resolved.is_file() or resolved in seen:
        return None
    if len(seen) >= MAX_PYTHON_FILES:
        raise AuditError(f"Python file count exceeds {MAX_PYTHON_FILES}")
    seen.add(resolved)
    return resolved


def _iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    supplied_paths = tuple(islice(paths, MAX_INPUT_PATHS + 1))
    if len(supplied_paths) > MAX_INPUT_PATHS:
        raise AuditError(f"audit accepts at most {MAX_INPUT_PATHS} input paths")
    seen: set[Path] = set()
    directories: list[Path] = []
    discovered = 0
    # quality: ignore[POT02] - supplied_paths is rejected above MAX_INPUT_PATHS
    for supplied in supplied_paths:
        path = supplied.resolve()
        if not path.exists():
            raise AuditError(f"audit path does not exist: {supplied}")
        if not path.is_file() and not path.is_dir():
            raise AuditError(f"audit path is not a regular file or directory: {supplied}")
        if path.is_file() and path.suffix != ".py":
            raise AuditError(f"audit path is not Python source: {supplied}")
        if path.is_dir():
            # quality: ignore[POT03] - supplied directories are capped at MAX_INPUT_PATHS
            directories.append(path)
            continue
        registered = _register_python_file(path, seen)
        if registered is not None:
            yield registered
    for _ in range(MAX_DIRECTORY_VISITS):
        if not directories:
            return
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                # quality: ignore[POT02] - every entry is charged to the hard discovery cap
                for entry in entries:
                    discovered += 1
                    if discovered > MAX_DISCOVERED_PATHS:
                        raise AuditError(
                            f"discovered path count exceeds {MAX_DISCOVERED_PATHS}"
                        )
                    candidate = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in EXCLUDED_DIRECTORIES:
                            # quality: ignore[POT03] - one queued directory per capped entry
                            directories.append(candidate)
                    elif candidate.suffix == ".py" and entry.is_file():
                        registered = _register_python_file(candidate, seen)
                        if registered is not None:
                            yield registered
        except OSError as error:
            raise AuditError(f"cannot inspect audit directory {directory}: {error}") from error
    if directories:
        raise AuditError(f"directory traversal exceeds {MAX_DIRECTORY_VISITS} visits")


def _node_end(node: ast.AST) -> int:
    # quality: ignore[POT08] - ast.AST line metadata is optional by contract
    return int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))


def _is_suppressed(lines: SourceLines, line_number: int, code: str) -> bool:
    suppressions = lines.suppressions
    for candidate in (line_number, line_number - 1):
        if code in suppressions.get(candidate, ()):
            return True
    return False


def _collect_functions(tree: ast.Module) -> list[FunctionInfo]:
    functions: list[FunctionInfo] = []
    pending: list[tuple[ast.AST, tuple[str, ...], tuple[str, ...]]] = [
        (tree, (), ())
    ]
    for _ in range(MAX_AST_NODES):
        if not pending:
            return functions
        node, scope, class_stack = pending.pop()
        child_scope = scope
        child_class_stack = class_stack
        if isinstance(node, ast.ClassDef):
            child_scope = (*scope, node.name)
            child_class_stack = (*class_stack, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified_name = ".".join((*scope, node.name))
            class_name = ".".join(class_stack) or None
            functions.append(FunctionInfo(qualified_name, class_name, node))
            child_scope = (*scope, node.name)
        children = tuple(ast.iter_child_nodes(node))
        # quality: ignore[POT02] - the parsed tree was rejected above MAX_AST_NODES
        for child in reversed(children):
            if len(pending) >= MAX_AST_NODES:
                raise AuditError(f"AST worklist exceeds {MAX_AST_NODES} nodes")
            # quality: ignore[POT03] - pending is checked before every insertion
            pending.append((child, child_scope, child_class_stack))
    if pending:
        raise AuditError(f"AST traversal exceeds {MAX_AST_NODES} nodes")
    return functions


def _walk_without_nested_definitions(node: ast.AST) -> Iterator[ast.AST]:
    pending: deque[ast.AST] = deque(ast.iter_child_nodes(node))
    for _ in range(MAX_AST_NODES):
        if not pending:
            return
        current = pending.popleft()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield current
        # quality: ignore[POT02] - child count is checked against MAX_AST_NODES
        for child in ast.iter_child_nodes(current):
            if len(pending) >= MAX_AST_NODES:
                raise AuditError(f"AST worklist exceeds {MAX_AST_NODES} nodes")
            # quality: ignore[POT03] - pending is checked before every insertion
            pending.append(child)
    if pending:
        raise AuditError(f"AST traversal exceeds {MAX_AST_NODES} nodes")


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    pending: deque[ast.AST] = deque([tree])
    for _ in range(MAX_AST_NODES):
        if not pending:
            return parents
        parent = pending.popleft()
        # quality: ignore[POT02] - the outer MAX_AST_NODES loop caps all children
        for child in ast.iter_child_nodes(parent):
            if len(parents) >= MAX_AST_NODES - 1:
                raise AuditError(f"AST exceeds {MAX_AST_NODES} nodes")
            parents[child] = parent
            # quality: ignore[POT03] - parent count is checked before every insertion
            pending.append(child)
    if pending:
        raise AuditError(f"AST exceeds {MAX_AST_NODES} nodes")
    return parents


def _resolve_local_call(
    call: ast.Call,
    function: FunctionInfo,
    names: dict[str, list[str]],
) -> str | None:
    target = call.func
    if isinstance(target, ast.Name) and len(names.get(target.id, ())) == 1:
        return names[target.id][0]
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in {"self", "cls"}
        and function.class_name
    ):
        candidate = f"{function.class_name}.{target.attr}"
        return candidate
    return None


def _recursive_functions(functions: Sequence[FunctionInfo]) -> set[str]:
    names: dict[str, list[str]] = defaultdict(list)
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        # quality: ignore[POT03] - one name entry per bounded function
        names[function.node.name].append(function.qualified_name)
    graph: dict[str, set[str]] = {function.qualified_name: set() for function in functions}
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        # quality: ignore[POT02] - the helper enforces MAX_AST_NODES per function
        for node in _walk_without_nested_definitions(function.node):
            if isinstance(node, ast.Call):
                target = _resolve_local_call(node, function, names)
                if target in graph:
                    # quality: ignore[POT03] - graph edges are capped by source AST nodes
                    graph[function.qualified_name].add(target)
    recursive: set[str] = set()
    total_steps = 0
    # quality: ignore[POT02] - graph size is capped and total work has a hard ceiling
    for origin in graph:
        pending = deque(graph[origin])
        queued = set(pending)
        for _ in range(MAX_AST_NODES):
            if not pending:
                break
            total_steps += 1
            if total_steps > MAX_CALL_GRAPH_STEPS:
                raise AuditError(
                    f"call graph traversal exceeds {MAX_CALL_GRAPH_STEPS} steps"
                )
            current = pending.popleft()
            if current == origin:
                # quality: ignore[POT03] - recursive members cannot exceed graph nodes
                recursive.add(origin)
                break
            # quality: ignore[POT02] - graph edges derive from the bounded source AST
            for target in graph.get(current, ()):
                if target not in queued:
                    # quality: ignore[POT03] - queued is capped by graph node count
                    queued.add(target)
                    # quality: ignore[POT03] - each graph node is enqueued at most once
                    pending.append(target)
        else:
            raise AuditError(f"call graph depth exceeds {MAX_AST_NODES} functions")
    return recursive


def _numeric_constant(node: ast.AST) -> float | None:
    current = node
    sign = 1.0
    for _ in range(MAX_NUMERIC_UNARY_DEPTH):
        if not isinstance(current, ast.UnaryOp):
            break
        if isinstance(current.op, ast.USub):
            sign *= -1
        elif not isinstance(current.op, ast.UAdd):
            return None
        current = current.operand
    if isinstance(current, ast.UnaryOp):
        return None
    if (
        isinstance(current, ast.Constant)
        and isinstance(current.value, (int, float))
        and not isinstance(current.value, bool)
    ):
        return sign * float(current.value)
    return None


def _is_preset_number(node: ast.AST) -> bool:
    return _numeric_constant(node) is not None or (
        isinstance(node, ast.Name) and node.id.isupper()
    )


def _previous_statement(
    node: ast.stmt, parents: dict[ast.AST, ast.AST]
) -> ast.stmt | None:
    parent = parents.get(node)
    if parent is None:
        return None
    # quality: ignore[POT02] - ast.iter_fields has a fixed schema per AST node type
    for _, value in ast.iter_fields(parent):
        if not isinstance(value, list) or node not in value:
            continue
        index = value.index(node)
        return value[index - 1] if index > 0 else None
    return None


def _has_preset_initial_value(statement: ast.stmt | None, name: str) -> bool:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        return (
            isinstance(target, ast.Name)
            and target.id == name
            and _is_preset_number(statement.value)
        )
    if isinstance(statement, ast.AnnAssign):
        target = statement.target
        return (
            isinstance(target, ast.Name)
            and target.id == name
            and statement.value is not None
            and _is_preset_number(statement.value)
        )
    return False


def _has_unconditional_counter_update(
    node: ast.While, name: str, increasing: bool
) -> bool:
    updates = [
        statement
        for statement in node.body
        if isinstance(statement, ast.AugAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == name
    ]
    if len(updates) != 1:
        return False
    update = updates[0]
    amount = _numeric_constant(update.value)
    correct_direction = (
        increasing and isinstance(update.op, ast.Add)
    ) or (
        not increasing and isinstance(update.op, ast.Sub)
    )
    if amount is None or amount <= 0 or not correct_direction:
        return False
    writes = [
        child
        for child in _walk_without_nested_definitions(node)
        if isinstance(child, ast.Name)
        and isinstance(child.ctx, ast.Store)
        and child.id == name
    ]
    has_continue = any(
        isinstance(child, ast.Continue)
        for child in _walk_without_nested_definitions(node)
    )
    return len(writes) == 1 and writes[0] is update.target and not has_continue


def _while_is_statically_bounded(
    node: ast.While, parents: dict[ast.AST, ast.AST]
) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.left, ast.Name):
        return False
    bound = test.comparators[0]
    if not _is_preset_number(bound):
        return False
    previous = _previous_statement(node, parents)
    if not _has_preset_initial_value(previous, test.left.id):
        return False
    if isinstance(test.ops[0], (ast.Lt, ast.LtE)):
        return _has_unconditional_counter_update(node, test.left.id, increasing=True)
    if isinstance(test.ops[0], (ast.Gt, ast.GtE)):
        return _has_unconditional_counter_update(node, test.left.id, increasing=False)
    return False


def _iterable_bound_kind(node: ast.AST) -> str | None:
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return "fixed" if not any(isinstance(item, ast.Starred) for item in node.elts) else None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        upper = node.slice.upper
        capped = upper is not None and _is_preset_number(upper)
        return "truncating" if capped else None
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node.func)
    if name == "range" and node.args:
        fixed = len(node.args) <= 3 and all(_is_preset_number(item) for item in node.args)
        return "fixed" if fixed else None
    if name.endswith("islice") and len(node.args) >= 2:
        stop_index = 1 if len(node.args) == 2 else 2
        bound = node.args[stop_index] if stop_index < len(node.args) else None
        capped = bound is not None and _is_preset_number(bound)
        return "truncating" if capped else None
    return None


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    for _ in range(MAX_CHAIN_DEPTH):
        if not isinstance(current, ast.Attribute):
            break
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _mutable_default(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Set)):
        return True
    return isinstance(node, ast.Call) and _call_name(node.func).split(".")[-1] in MUTABLE_BUILDERS


def _meaningful_check_count(function: FunctionInfo) -> int:
    count = 0
    # quality: ignore[POT02] - the helper enforces MAX_AST_NODES per function
    for node in _walk_without_nested_definitions(function.node):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.If) and any(isinstance(item, ast.Raise) for item in node.body):
            count += 1
        elif isinstance(node, ast.Call):
            name = _call_name(node.func).split(".")[-1].lower()
            if name.startswith(("check", "ensure", "require", "validate")):
                count += 1
    return count


def _find_project_config(start: Path) -> Path | None:
    current = start.resolve()
    for _ in range(MAX_CONFIG_SEARCH_DEPTH):
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
    return None


def _read_config_table(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    if len(payload) > MAX_CONFIG_BYTES:
        raise AuditError(f"{path} exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AuditError(f"cannot parse {path}: {error}") from error
    tools = document.get("tool")
    section = tools.get(CONFIG_TABLE) if isinstance(tools, dict) else None
    return section if isinstance(section, dict) else {}


def _validated_style(value: object, origin: str) -> str:
    if not isinstance(value, str) or value.lower() not in DOCSTRING_STYLES:
        supported = ", ".join(DOCSTRING_STYLES)
        raise AuditError(
            f"unknown docstring style {value!r} in {origin}; expected one of {supported}"
        )
    return value.lower()


def _configured_style(start: Path) -> tuple[str, str]:
    config_path = _find_project_config(start)
    if config_path is None:
        return DEFAULT_DOCSTRING_STYLE, "built-in default"
    if tomllib is None:
        return (
            DEFAULT_DOCSTRING_STYLE,
            f"built-in default; reading {CONFIG_FILENAME} requires Python 3.11 or newer",
        )
    value = _read_config_table(config_path).get(CONFIG_KEY)
    if value is None:
        return DEFAULT_DOCSTRING_STYLE, "built-in default"
    origin = f"{config_path} [tool.{CONFIG_TABLE}] {CONFIG_KEY}"
    return _validated_style(value, origin), str(config_path)


def resolve_docstring_style(
    requested: str | None, environment: Mapping[str, str], start: Path
) -> tuple[str, str]:
    """Resolve the documentation convention from all configuration sources.

    Precedence is the command-line flag, then the environment variable, then
    ``[tool.reliable-python] docstring-style`` in the nearest `pyproject.toml`
    at or above `start`, then the built-in default.

    Parameters
    ----------
    requested : str or None
        Value supplied on the command line, or ``None`` when the flag was
        omitted.
    environment : mapping of str to str
        Process environment consulted for `DOCSTYLE_ENV_VAR`.
    start : Path
        Directory the `pyproject.toml` search begins from, walking upward.

    Returns
    -------
    tuple of (str, str)
        The resolved style and a human-readable description of where it came
        from, suitable for reporting to a user who is debugging configuration.

    Raises
    ------
    AuditError
        If any source supplies a style outside `DOCSTRING_STYLES`, or the
        configuration file cannot be read or parsed.
    """
    if requested is not None:
        return _validated_style(requested, "--docstring-style"), "--docstring-style"
    from_environment = environment.get(DOCSTYLE_ENV_VAR)
    if from_environment is not None:
        return _validated_style(from_environment, DOCSTYLE_ENV_VAR), DOCSTYLE_ENV_VAR
    return _configured_style(start)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def _docstring_text_lines(text: str) -> tuple[str, ...]:
    lines = text.expandtabs(8).splitlines()
    if len(lines) > MAX_DOCSTRING_LINES:
        raise AuditError(f"docstring exceeds {MAX_DOCSTRING_LINES} lines")
    return tuple(lines)


def _is_numpy_underline(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) == {"-"}


def _is_google_heading(current: str, following: str) -> bool:
    stripped = current.strip()
    if not stripped.endswith(":") or stripped[:-1] not in GOOGLE_SECTION_NAMES:
        return False
    if not following.strip():
        return False
    return _indent_width(following) > _indent_width(current)


def _numpy_section_hits(lines: Sequence[str]) -> int:
    return sum(
        1
        for index in range(len(lines) - 1)
        if lines[index].strip() in NUMPY_SECTION_NAMES
        and _is_numpy_underline(lines[index + 1])
    )


def _google_section_hits(lines: Sequence[str]) -> int:
    return sum(
        1
        for index in range(len(lines) - 1)
        if _is_google_heading(lines[index], lines[index + 1])
    )


def _rest_field_hits(lines: Sequence[str]) -> int:
    return sum(1 for line in lines if REST_FIELD_RE.match(line))


def detect_docstring_style(text: str) -> str | None:
    """Identify the section convention a docstring positively demonstrates.

    A summary-only docstring carries no section markers and is valid in every
    supported convention, so it is reported as undetermined rather than as a
    violation.

    Parameters
    ----------
    text : str
        Raw docstring body, without the surrounding quotes.

    Returns
    -------
    str or None
        ``"numpy"``, ``"google"``, or ``"rest"`` when exactly one convention
        supplies the most evidence, and ``None`` when the docstring has no
        sections or two conventions tie.

    Raises
    ------
    AuditError
        If the docstring exceeds ``MAX_DOCSTRING_LINES`` lines.
    """
    lines = _docstring_text_lines(text)
    scores = {
        "numpy": _numpy_section_hits(lines),
        "google": _google_section_hits(lines),
        "rest": _rest_field_hits(lines),
    }
    best = max(scores.values())
    if best == 0:
        return None
    leaders = [name for name, score in scores.items() if score == best]
    return leaders[0] if len(leaders) == 1 else None


def _numpy_section_body(lines: Sequence[str], name: str) -> tuple[str, ...]:
    start = -1
    # quality: ignore[POT02] - docstring lines are capped at MAX_DOCSTRING_LINES
    for index in range(len(lines) - 1):
        heading = lines[index].strip()
        if not _is_numpy_underline(lines[index + 1]):
            continue
        if start < 0 and heading == name:
            start = index + 2
        elif start >= 0 and heading in NUMPY_SECTION_NAMES:
            return tuple(lines[start:index])
    return tuple(lines[start:]) if start >= 0 else ()


def _google_section_body(lines: Sequence[str], names: frozenset[str]) -> tuple[str, ...]:
    start = -1
    heading_indent = 0
    # quality: ignore[POT02] - docstring lines are capped at MAX_DOCSTRING_LINES
    for index in range(len(lines)):
        stripped = lines[index].strip()
        if not stripped.endswith(":") or stripped[:-1] not in GOOGLE_SECTION_NAMES:
            continue
        if start < 0 and stripped[:-1] in names:
            start = index + 1
            heading_indent = _indent_width(lines[index])
        elif start >= 0 and _indent_width(lines[index]) <= heading_indent:
            return tuple(lines[start:index])
    return tuple(lines[start:]) if start >= 0 else ()


def _split_parameter_names(head: str) -> set[str]:
    candidates = [part.strip().lstrip("*") for part in head.split(",")]
    return {name for name in candidates if name.isidentifier()}


def _entry_heads(body: Sequence[str], separator: str) -> list[str]:
    entries = [line for line in body if line.strip()]
    if not entries:
        return []
    base = min(_indent_width(line) for line in entries)
    return [
        line.split(separator)[0].strip()
        for line in entries
        if _indent_width(line) == base and separator in line
    ]


def _numpy_documented_parameters(lines: Sequence[str]) -> set[str]:
    body = _numpy_section_body(lines, "Parameters")
    entries = [line for line in body if line.strip()]
    if not entries:
        return set()
    base = min(_indent_width(line) for line in entries)
    heads = [
        line.split(" :")[0].strip() if " :" in line else line.strip()
        for line in entries
        if _indent_width(line) == base
    ]
    return {name for head in heads for name in _split_parameter_names(head)}


def _google_documented_parameters(lines: Sequence[str]) -> set[str]:
    body = _google_section_body(lines, GOOGLE_PARAMETER_SECTIONS)
    heads = [head.split("(")[0].strip() for head in _entry_heads(body, ":")]
    return {name for head in heads for name in _split_parameter_names(head)}


def _rest_documented_parameters(lines: Sequence[str]) -> set[str]:
    matches = [REST_PARAMETER_RE.match(line) for line in lines]
    return {
        match.group("name").lstrip("*") for match in matches if match is not None
    }


def _documented_parameters(text: str, style: str) -> set[str]:
    lines = _docstring_text_lines(text)
    if style == "numpy":
        return _numpy_documented_parameters(lines)
    if style == "google":
        return _google_documented_parameters(lines)
    if style == "rest":
        return _rest_documented_parameters(lines)
    return set()


def _signature_parameter_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    arguments = node.args
    named = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    extras = [item for item in (arguments.vararg, arguments.kwarg) if item is not None]
    return tuple(
        item.arg for item in [*named, *extras] if item.arg not in {"self", "cls"}
    )


def _is_docstring_statement(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _docstring_span(node: ast.AST) -> int:
    # quality: ignore[POT08] - only definition nodes carry an optional body field
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return 0
    first = body[0]
    if not isinstance(first, ast.stmt) or not _is_docstring_statement(first):
        return 0
    return _node_end(first) - first.lineno + 1


def _docstring_anchor(node: ast.AST) -> ast.AST:
    # quality: ignore[POT08] - only definition nodes carry an optional body field
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and _is_docstring_statement(body[0]):
        return body[0]
    return node


def _is_test_module(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _is_nested_function(function: FunctionInfo) -> bool:
    depth = function.qualified_name.count(".") + 1
    class_depth = function.class_name.count(".") + 1 if function.class_name else 0
    return depth != class_depth + 1


def _is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [item for item in node.body if not _is_docstring_statement(item)]
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, ast.Pass):
        return True
    return (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    )


def _has_exempt_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    names = [
        _call_name(item.func) if isinstance(item, ast.Call) else _call_name(item)
        for item in node.decorator_list
    ]
    return any(name.split(".")[-1] in DOCSTRING_EXEMPT_DECORATORS for name in names)


def _is_public_scope(function: FunctionInfo) -> bool:
    owner = function.class_name
    if owner is None:
        return True
    return not any(part.startswith("_") for part in owner.split("."))


def _requires_docstring(path: Path, function: FunctionInfo) -> bool:
    node = function.node
    name = node.name
    if name.startswith("_") or not _is_public_scope(function):
        return False
    if _is_nested_function(function) or _is_stub_body(node):
        return False
    if _has_exempt_decorator(node):
        return False
    return not (_is_test_module(path) and name.startswith("test"))


def _invalid_suppressions(lines: SourceLines, path: Path) -> list[Finding]:
    findings: list[Finding] = []
    numbers = sorted(lines.invalid_suppressions)
    # quality: ignore[POT02] - overflow beyond MAX_FINDINGS is reported below
    for number in numbers[:MAX_FINDINGS]:
        findings.append(
            Finding(
                "QLT001",
                "error",
                str(path),
                number,
                number,
                "malformed quality suppression; a rule ID and specific rationale are required",
                "Use '# quality: ignore[POT02] - specific reason' or remove the marker.",
            )
        )
    if len(numbers) > MAX_FINDINGS:
        _record_limit(
            findings,
            path,
            f"finding count exceeds {MAX_FINDINGS}",
            "Remove malformed suppressions and audit the source again.",
        )
    return findings


def _check_function_signature(
    context: ReviewContext, function: FunctionInfo
) -> None:
    node = function.node
    positional = [*node.args.posonlyargs, *node.args.args]
    parameter_count = len(positional) + len(node.args.kwonlyargs)
    if positional and positional[0].arg in {"self", "cls"}:
        parameter_count -= 1
    if parameter_count > 5:
        context.report(
            code="CS04",
            severity="warning",
            node=node,
            message=(
                f"function {function.qualified_name} has "
                f"{parameter_count} parameters"
            ),
            remedy=(
                "Introduce a cohesive parameter object or preserve an existing "
                "domain object."
            ),
        )
    defaults = [*node.args.defaults, *node.args.kw_defaults]
    has_mutable_default = any(
        default is not None and _mutable_default(default) for default in defaults
    )
    if has_mutable_default:
        context.report(
            code="POT06",
            severity="error",
            node=node,
            message=f"function {function.qualified_name} has a mutable default argument",
            remedy="Use None and allocate the value inside the function.",
        )


def _check_functions(
    context: ReviewContext, functions: Sequence[FunctionInfo]
) -> None:
    recursive = _recursive_functions(functions)
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        node = function.node
        if function.qualified_name in recursive:
            context.report(
                code="POT01",
                severity="error",
                node=node,
                message=f"recursive call cycle includes {function.qualified_name}",
                remedy="Replace recursion with an explicitly bounded worklist.",
            )
        span = _node_end(node) - node.lineno + 1 - _docstring_span(node)
        if span > 60:
            context.report(
                code="POT04",
                severity="error",
                node=node,
                message=(
                    f"function {function.qualified_name} spans {span} code lines "
                    "excluding its docstring (limit: 60)"
                ),
                remedy="Extract coherent units without compressing statements.",
            )
        _check_function_signature(context, function)
    if functions:
        total_checks = sum(_meaningful_check_count(function) for function in functions)
        density = total_checks / len(functions)
        if density < 2:
            first = functions[0].node
            context.report(
                code="POT05",
                severity="warning",
                node=first,
                message=(
                    f"defensive-check density is {density:.2f} per function "
                    "(target average: 2.00)"
                ),
                remedy=(
                    "Add meaningful boundary checks or document why trivial "
                    "functions need none."
                ),
            )


def _report_missing_docstring(
    context: ReviewContext, node: ast.AST, subject: str
) -> None:
    label = STYLE_LABELS.get(context.docstring_style, "NumPy")
    context.report(
        code="DOC01",
        severity="warning",
        node=node,
        message=f"public {subject} has no docstring",
        remedy=(
            f"Add a {label}-style docstring, or make the name private if it is "
            "not part of the public surface."
        ),
    )


def _report_docstring_style(
    context: ReviewContext, text: str, node: ast.AST, subject: str
) -> None:
    if context.docstring_style == "any":
        return
    detected = detect_docstring_style(text)
    if detected is None or detected == context.docstring_style:
        return
    expected = STYLE_LABELS[context.docstring_style]
    context.report(
        code="DOC02",
        severity="warning",
        node=node,
        message=(
            f"docstring for {subject} uses {STYLE_LABELS[detected]} sections; "
            f"this project documents in {expected} style"
        ),
        remedy=(
            f"Rewrite the sections in {expected} form, or audit with "
            f"--docstring-style {detected} if the project standard changed."
        ),
    )


def _report_parameter_gaps(
    context: ReviewContext, text: str, node: ast.AST, function: FunctionInfo
) -> None:
    detected = detect_docstring_style(text)
    if detected is None:
        return
    documented = _documented_parameters(text, detected)
    if not documented:
        return
    expected = _signature_parameter_names(function.node)
    missing = [name for name in expected if name not in documented]
    if not missing:
        return
    context.report(
        code="DOC03",
        severity="warning",
        node=node,
        message=(
            f"docstring for {function.qualified_name} documents "
            f"{len(expected) - len(missing)} of {len(expected)} parameters; "
            f"missing {', '.join(missing)}"
        ),
        remedy=(
            "Document every parameter, or drop the parameter section so the "
            "docstring does not look complete when it is not."
        ),
    )


def _check_function_docstring(context: ReviewContext, function: FunctionInfo) -> None:
    node = function.node
    if not _requires_docstring(context.path, function):
        return
    text = ast.get_docstring(node, clean=False)
    if text is None or not text.strip():
        _report_missing_docstring(context, node, f"function {function.qualified_name}")
        return
    anchor = _docstring_anchor(node)
    _report_docstring_style(context, text, anchor, f"function {function.qualified_name}")
    _report_parameter_gaps(context, text, anchor, function)


def _check_module_docstring(context: ReviewContext) -> None:
    if context.path.name == "__init__.py" or not context.tree.body:
        return
    text = ast.get_docstring(context.tree, clean=False)
    subject = f"module {context.path.name}"
    if text is None or not text.strip():
        _report_missing_docstring(context, context.tree.body[0], subject)
        return
    _report_docstring_style(context, text, _docstring_anchor(context.tree), subject)


def _check_class_docstrings(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        text = ast.get_docstring(node, clean=False)
        subject = f"class {node.name}"
        if text is None or not text.strip():
            _report_missing_docstring(context, node, subject)
            continue
        _report_docstring_style(context, text, _docstring_anchor(node), subject)


def _check_docstrings(
    context: ReviewContext, functions: Sequence[FunctionInfo]
) -> None:
    _check_module_docstring(context)
    _check_class_docstrings(context)
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        _check_function_docstring(context, function)


def _check_loops(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        bounded = True
        if isinstance(node, ast.While):
            bounded = _while_is_statically_bounded(node, context.parents)
            if not bounded:
                context.report(
                    code="POT02",
                    severity="error",
                    node=node,
                    message="while loop has no mechanically visible preset upper bound",
                    remedy="Use a named maximum and fail explicitly when it is exhausted.",
                )
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound_kind = _iterable_bound_kind(node.iter) if isinstance(node, ast.For) else None
            bounded = bound_kind is not None
            if not bounded:
                context.report(
                    code="POT02",
                    severity="warning",
                    node=node,
                    message="iteration bound depends on runtime data",
                    remedy=(
                        "Cap the iterable with a named maximum or justify an "
                        "intentional service loop."
                    ),
                )
            elif bound_kind == "truncating":
                context.report(
                    code="POT02",
                    severity="warning",
                    node=node,
                    message="iteration is capped but may silently discard input beyond the limit",
                    remedy="Reject oversized input or return an explicit truncation/error signal.",
                )
        else:
            continue
        if bounded:
            continue
        # quality: ignore[POT02] - node belongs to the bounded context.tree AST
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr in MUTATING_METHODS:
                    context.report(
                        code="POT03",
                        severity="warning",
                        node=child,
                        message=(
                            f"{child.func.attr} grows state inside a loop without "
                            "a proven bound"
                        ),
                        remedy=(
                            "Bound the loop and resource, stream results, or use "
                            "a bounded container."
                        ),
                    )


def _check_exceptions_and_dynamic_code(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        if isinstance(node, ast.ExceptHandler):
            is_broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in {"BaseException", "Exception"}
            )
            pass_only = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if is_broad and pass_only:
                context.report(
                    code="POT07",
                    severity="error",
                    node=node,
                    message="broad exception handler silently discards the failure",
                    remedy=(
                        "Catch the expected exception and recover or propagate it "
                        "with context."
                    ),
                )
            elif node.type is None:
                context.report(
                    code="POT07",
                    severity="warning",
                    node=node,
                    message="bare except catches control-flow and process-level exceptions",
                    remedy="Catch only the exceptions this boundary can handle.",
                )
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        is_builtin_name = isinstance(node.func, ast.Name) or name.startswith("builtins.")
        leaf = name.split(".")[-1]
        if is_builtin_name and leaf in DYNAMIC_BUILTINS:
            context.report(
                code="POT08",
                severity="error",
                node=node,
                message=f"dynamic code operation {name} hides behavior from static analysis",
                remedy="Use explicit parsing, dispatch tables, imports, or ordinary functions.",
            )
        elif is_builtin_name and leaf in REFLECTION_BUILTINS:
            context.report(
                code="POT08",
                severity="warning",
                node=node,
                message=f"reflective operation {name} obscures the accessed interface",
                remedy="Prefer explicit typed access or justify the framework boundary.",
            )


def _attribute_depth(node: ast.Attribute) -> int:
    depth = 1
    current = node.value
    for _ in range(MAX_CHAIN_DEPTH):
        if not isinstance(current, ast.Attribute):
            break
        depth += 1
        current = current.value
    return depth


def _check_indirection_and_privacy(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.Attribute):
            continue
        if isinstance(context.parents.get(node), ast.Attribute):
            continue
        depth = _attribute_depth(node)
        if depth >= 3:
            context.report(
                code="POT09",
                severity="warning",
                node=node,
                message=f"attribute chain has {depth} levels of indirection",
                remedy=(
                    "Add a named boundary/query or use intermediate values with "
                    "clear ownership."
                ),
            )
        root = node.value
        reaches_foreign_private_state = (
            node.attr.startswith("_")
            and isinstance(root, ast.Name)
            and root.id not in {"self", "cls"}
        )
        if reaches_foreign_private_state:
            context.report(
                code="CS20",
                severity="warning",
                node=node,
                message=f"code reaches into private state {root.id}.{node.attr}",
                remedy="Expose a narrow operation or move the behavior to the data owner.",
            )


def _if_chain_length(node: ast.If) -> int:
    count = 1
    current = node
    for _ in range(MAX_CHAIN_DEPTH):
        if len(current.orelse) != 1 or not isinstance(current.orelse[0], ast.If):
            break
        count += 1
        current = current.orelse[0]
    return count


def _class_assignments(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    # quality: ignore[POT02] - node belongs to a tree capped at MAX_AST_NODES
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            # quality: ignore[POT02] - assignment target count is capped by AST size
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self":
                        # quality: ignore[POT03] - field names cannot exceed AST nodes
                        names.add(target.attr)
    return names


def _is_forwarder(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = node.body
    if len(body) != 1 or node.name.startswith("__"):
        return False
    statement = body[0]
    value = statement.value if isinstance(statement, (ast.Expr, ast.Return)) else None
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)


def _check_conditional(context: ReviewContext, node: ast.AST) -> None:
    parent = context.parents.get(node)
    is_elif = (
        isinstance(parent, ast.If)
        and len(parent.orelse) == 1
        and parent.orelse[0] is node
    )
    if isinstance(node, ast.If) and not is_elif:
        length = _if_chain_length(node)
        if length >= 4:
            context.report(
                code="CS08",
                severity="warning",
                node=node,
                message=f"conditional dispatch has {length} branches",
                remedy=(
                    "Centralize the closed dispatch or use a typed strategy "
                    "when variants recur."
                ),
            )
    elif isinstance(node, ast.Match) and len(node.cases) >= 5:
        context.report(
            code="CS08",
            severity="warning",
            node=node,
            message=f"match dispatch has {len(node.cases)} branches",
            remedy=(
                "Keep one exhaustive closed-domain dispatch or move recurring "
                "behavior to variants."
            ),
        )


def _check_class(context: ReviewContext, node: ast.ClassDef) -> None:
    methods = [
        item
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    span = _node_end(node) - node.lineno + 1
    if span > 300 or len(methods) > 20:
        context.report(
            code="CS02",
            severity="warning",
            node=node,
            message=f"class {node.name} spans {span} lines and {len(methods)} methods",
            remedy="Split only along demonstrated responsibility or change boundaries.",
        )
    public_methods = [method for method in methods if not method.name.startswith("_")]
    assignments = _class_assignments(node)
    if len(assignments) >= 4 and len(public_methods) <= 1:
        context.report(
            code="CS15",
            severity="warning",
            node=node,
            message=f"class {node.name} mainly exposes {len(assignments)} data fields",
            remedy=(
                "Move relevant invariants/behavior here, unless this is an "
                "intentional DTO or schema."
            ),
        )
    forwarders = [method for method in public_methods if _is_forwarder(method)]
    if len(forwarders) >= 3 and len(forwarders) * 2 >= len(public_methods):
        context.report(
            code="CS23",
            severity="warning",
            node=node,
            message=f"class {node.name} mostly forwards {len(forwarders)} public methods",
            remedy=(
                "Remove the middle layer unless it protects a real policy or "
                "compatibility boundary."
            ),
        )


def _check_classes_and_conditionals(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        _check_conditional(context, node)
        if isinstance(node, ast.ClassDef):
            _check_class(context, node)


def _check_dead_code(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for parent in ast.walk(context.tree):
        for field in ("body", "orelse", "finalbody"):
            # quality: ignore[POT08] - these optional AST statement fields are fixed by schema
            body = getattr(parent, field, None)
            if not isinstance(body, list):
                continue
            terminated = False
            # quality: ignore[POT02] - body statements are capped by MAX_AST_NODES
            for statement in body:
                if terminated:
                    context.report(
                        code="CS16",
                        severity="warning",
                        node=statement,
                        message="statement is unreachable after an unconditional terminator",
                        remedy=(
                            "Delete the dead statement after confirming no generated/"
                            "reflection entry point."
                        ),
                    )
                    break
                terminated = isinstance(statement, TERMINATORS)


def _check_data_clumps(
    context: ReviewContext, functions: Sequence[FunctionInfo]
) -> None:
    groups: dict[tuple[str, ...], list[FunctionInfo]] = defaultdict(list)
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        node_args = function.node.args
        parameters = [
            arg.arg
            for arg in [*node_args.posonlyargs, *node_args.args, *node_args.kwonlyargs]
            if arg.arg not in {"self", "cls"}
        ]
        if len(parameters) >= 3:
            # quality: ignore[POT03] - groups cannot exceed the bounded function count
            groups[tuple(parameters[:3])].append(function)
    # quality: ignore[POT02] - groups cannot exceed the bounded function count
    for names, owners in groups.items():
        if len(owners) < 3:
            continue
        node = owners[0].node
        context.report(
            code="CS05",
            severity="warning",
            node=node,
            message=(
                f"parameters {', '.join(names)} recur together in "
                f"{len(owners)} functions"
            ),
            remedy=(
                "Introduce a domain value only if the values share invariants "
                "and change together."
            ),
        )


def _check_duplicate_bodies(
    context: ReviewContext, functions: Sequence[FunctionInfo]
) -> None:
    seen: dict[str, FunctionInfo] = {}
    # quality: ignore[POT02] - functions comes from an AST capped at MAX_AST_NODES
    for function in functions:
        if len(function.node.body) < 2:
            continue
        module = ast.Module(body=function.node.body, type_ignores=[])
        fingerprint = ast.dump(module, include_attributes=False)
        previous = seen.get(fingerprint)
        if previous is None:
            seen[fingerprint] = function
            continue
        context.report(
            code="CS14",
            severity="warning",
            node=function.node,
            message=f"function body duplicates {previous.qualified_name}",
            remedy="Extract shared knowledge if both copies have the same change reason.",
        )


def _check_module_scope(context: ReviewContext) -> None:
    # quality: ignore[POT02] - context.tree was rejected above MAX_AST_NODES
    for node in ast.walk(context.tree):
        if isinstance(node, ast.Global):
            context.report(
                code="POT06",
                severity="error",
                node=node,
                message=f"global statement widens mutation scope for {', '.join(node.names)}",
                remedy="Return the value, pass explicit state, or encapsulate ownership.",
            )
    # quality: ignore[POT02] - module body is capped by MAX_AST_NODES
    for node in context.tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not _mutable_default(value):
            continue
        context.report(
            code="POT06",
            severity="warning",
            node=node,
            message="module-level mutable state has process-wide scope",
            remedy=(
                "Use immutable configuration or encapsulate the state behind "
                "an explicit owner."
            ),
        )


def _source_boundary_finding(source: str, path: Path) -> Finding | None:
    if len(source) > MAX_SOURCE_BYTES:
        return _limit_finding(
            path,
            f"source exceeds {MAX_SOURCE_BYTES} UTF-8 bytes",
            "Audit a smaller file or split the module at a cohesive boundary.",
        )
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeEncodeError as error:
        return Finding(
            "IO001",
            "error",
            str(path),
            1,
            1,
            f"cannot encode Python source as UTF-8: {error}",
            "Provide valid Unicode source text.",
        )
    if source_size > MAX_SOURCE_BYTES:
        return _limit_finding(
            path,
            f"source exceeds {MAX_SOURCE_BYTES} UTF-8 bytes",
            "Audit a smaller file or split the module at a cohesive boundary.",
        )
    return None


def _parse_module(source: str, path: Path, findings: list[Finding]) -> ast.Module | None:
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        line = error.lineno or 1
        _record_finding(
            findings,
            Finding("PARSE001", "error", str(path), line, line,
                    f"Python syntax error: {error.msg}", "Fix syntax before quality review.")
        )
        return None


def _run_checks(context: ReviewContext, functions: Sequence[FunctionInfo]) -> None:
    _check_functions(context, functions)
    _check_docstrings(context, functions)
    _check_loops(context)
    _check_exceptions_and_dynamic_code(context)
    _check_indirection_and_privacy(context)
    _check_classes_and_conditionals(context)
    _check_dead_code(context)
    _check_data_clumps(context, functions)
    _check_duplicate_bodies(context, functions)
    _check_module_scope(context)


def analyze_source(
    source: str, path: Path, docstring_style: str = DEFAULT_DOCSTRING_STYLE
) -> list[Finding]:
    """Audit one Python source unit and return its deduplicated findings.

    Parameters
    ----------
    source : str
        Complete Python source text for `path`.
    path : Path
        Location reported with each finding. The file is not read again.
    docstring_style : str, default "numpy"
        Documentation convention the file is expected to follow. Use ``"any"``
        to accept every supported convention and disable DOC02.

    Returns
    -------
    list of Finding
        Findings sorted by line, then rule code, then message. Parse and limit
        failures are returned as findings rather than raised.
    """
    if docstring_style not in DOCSTRING_STYLES:
        raise AuditError(f"unknown docstring style: {docstring_style}")
    boundary_finding = _source_boundary_finding(source, path)
    if boundary_finding is not None:
        return [boundary_finding]
    lines = SourceLines(source)
    findings = _invalid_suppressions(lines, path)
    tree = _parse_module(source, path, findings)
    if tree is None:
        return findings
    try:
        parents = _build_parent_map(tree)
        functions = _collect_functions(tree)
    except AuditError as error:
        remedy = "Reduce the source unit or audit a narrower scope."
        _record_limit(findings, path, str(error), remedy)
        return findings
    context = ReviewContext(tree, lines, path, parents, findings, docstring_style)
    try:
        _run_checks(context, functions)
    except AuditError as error:
        _record_limit(
            findings,
            path,
            str(error),
            "Audit a narrower source unit or reduce its analysis complexity.",
        )
    return sorted(set(findings), key=lambda item: (item.line, item.code, item.message))


def _analyze_path(
    path: Path, remaining_bytes: int, docstring_style: str
) -> tuple[list[Finding], int]:
    if remaining_bytes <= 0:
        return ([
            _limit_finding(
                path,
                f"audit source total exceeds {MAX_TOTAL_SOURCE_BYTES} bytes",
                "Audit a narrower path set or split the scan into explicit bounded scopes.",
            )
        ], 0)
    read_limit = min(MAX_SOURCE_BYTES, remaining_bytes)
    try:
        with path.open("rb") as handle:
            payload = handle.read(read_limit + 1)
    except OSError as error:
        return ([
            Finding("IO001", "error", str(path), 1, 1,
                    f"cannot read Python source: {error}", "Make the file readable UTF-8 source.")
        ], 0)
    if len(payload) > read_limit:
        if read_limit < MAX_SOURCE_BYTES:
            message = f"audit source total exceeds {MAX_TOTAL_SOURCE_BYTES} bytes"
            remedy = "Audit a narrower path set or split the scan into explicit bounded scopes."
        else:
            message = f"source exceeds {MAX_SOURCE_BYTES} UTF-8 bytes"
            remedy = "Audit a smaller file or split the module at a cohesive boundary."
        return ([_limit_finding(path, message, remedy)], len(payload))
    try:
        source = payload.decode("utf-8")
    except UnicodeError as error:
        return ([
            Finding("IO001", "error", str(path), 1, 1,
                    f"cannot decode Python source: {error}", "Make the file readable UTF-8 source.")
        ], len(payload))
    return analyze_source(source, path, docstring_style), len(payload)


def analyze_path(
    path: Path, docstring_style: str = DEFAULT_DOCSTRING_STYLE
) -> list[Finding]:
    """Read one Python file and audit it in isolation.

    Parameters
    ----------
    path : Path
        Python file to read as UTF-8 and audit.
    docstring_style : str, default "numpy"
        Documentation convention passed through to `analyze_source`.

    Returns
    -------
    list of Finding
        Findings for this file, including read and decode failures.
    """
    return _analyze_path(path, MAX_SOURCE_BYTES, docstring_style)[0]


def _intersects_changed_lines(finding: Finding, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(finding.line <= end and finding.end_line >= start for start, end in ranges)


def filter_changed_findings(
    findings: Iterable[Finding], changed_lines: dict[Path, tuple[tuple[int, int], ...]]
) -> list[Finding]:
    """Keep only findings that intersect the changed lines.

    Parameters
    ----------
    findings : iterable of Finding
        Findings gathered across every audited file.
    changed_lines : dict
        Maps a resolved path to its inclusive changed line ranges.

    Returns
    -------
    list of Finding
        Findings overlapping a changed range, plus read, parse, and limit
        failures for any file in the change set.

    Raises
    ------
    AuditError
        If more than ``MAX_FINDINGS`` findings are supplied.
    """
    selected_findings = tuple(islice(findings, MAX_FINDINGS + 1))
    if len(selected_findings) > MAX_FINDINGS:
        raise AuditError(f"finding count exceeds {MAX_FINDINGS}")
    result: list[Finding] = []
    # quality: ignore[POT02] - selected_findings is rejected above MAX_FINDINGS
    for finding in selected_findings:
        selected_path = Path(finding.path).resolve()
        ranges = changed_lines.get(selected_path, ())
        infrastructure_error = finding.code in {"IO001", "LIMIT001", "PARSE001"}
        selected_error = infrastructure_error and selected_path in changed_lines
        if selected_error or _intersects_changed_lines(finding, ranges):
            # quality: ignore[POT03] - result cannot exceed MAX_FINDINGS
            result.append(finding)
    return result


def _summary(findings: Sequence[Finding]) -> dict[str, int]:
    counts = Counter(finding.severity for finding in findings)
    return {name: counts.get(name, 0) for name in ("error", "warning", "note")}


def _print_text(findings: Sequence[Finding]) -> None:
    if not findings:
        print("quality audit: no findings in the selected Python scope")
        return
    # quality: ignore[POT02] - findings is capped at MAX_FINDINGS
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.severity} {finding.code}: "
            f"{finding.message}\n  remedy: {finding.remedy}"
        )
    counts = _summary(findings)
    print(f"quality audit: {counts['error']} error(s), {counts['warning']} warning(s)")


def _print_json(findings: Sequence[Finding]) -> None:
    payload = {
        "summary": _summary(findings),
        "findings": [asdict(item) for item in findings],
    }
    print(json.dumps(payload))


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Python files or directories to audit")
    parser.add_argument("--git-diff", action="store_true", help="audit changed Python lines in Git")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=("none", "error", "warning"), default="warning")
    parser.add_argument(
        "--docstring-style",
        choices=DOCSTRING_STYLES,
        default=None,
        help=(
            "expected docstring convention; overrides "
            f"{DOCSTYLE_ENV_VAR} and {CONFIG_FILENAME}. 'any' disables DOC02"
        ),
    )
    parser.add_argument(
        "--print-docstring-style",
        action="store_true",
        help="report the resolved docstring convention and its source, then exit",
    )
    args = parser.parse_args(argv)
    if args.git_diff and args.paths:
        parser.error("use --git-diff or explicit paths, not both")
    if not args.print_docstring_style and not args.git_diff and not args.paths:
        parser.error("provide --git-diff or at least one path")
    return args


def _print_resolved_style(style: str, source: str, output_format: str) -> int:
    if output_format == "json":
        payload = {"style": style, "label": STYLE_LABELS.get(style, style), "source": source}
        print(json.dumps(payload))
    else:
        print(f"{style} (source: {source})")
    return 0


def _audit_paths(paths: Sequence[Path], docstring_style: str) -> list[Finding]:
    findings: list[Finding] = []
    total_bytes = 0
    # quality: ignore[POT02] - paths is rejected above MAX_PYTHON_FILES
    for path in paths:
        remaining_bytes = MAX_TOTAL_SOURCE_BYTES - total_bytes
        if remaining_bytes <= 0:
            _record_limit(
                findings,
                path,
                f"audit source total exceeds {MAX_TOTAL_SOURCE_BYTES} bytes",
                "Audit a narrower path set or split the scan into bounded scopes.",
            )
            break
        path_findings, byte_count = _analyze_path(path, remaining_bytes, docstring_style)
        total_bytes += byte_count
        # quality: ignore[POT03] - findings is truncated explicitly at MAX_FINDINGS
        findings.extend(path_findings)
        if len(findings) > MAX_FINDINGS:
            findings = findings[:MAX_FINDINGS]
            _record_limit(
                findings,
                path,
                f"finding count exceeds {MAX_FINDINGS}",
                "Audit a narrower scope and resolve findings before continuing.",
            )
            break
        if byte_count > remaining_bytes:
            break
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Run the audit from the command line.

    The docstring convention is resolved before anything is audited, so a
    misconfigured style fails fast with an explanation rather than silently
    auditing against the wrong convention.

    Parameters
    ----------
    argv : sequence of str or None, optional
        Argument list without the program name. ``None`` reads `sys.argv`.

    Returns
    -------
    int
        ``0`` when no finding reaches the `--fail-on` threshold, ``1`` when one
        does, and ``2`` when the audit could not be completed.
    """
    args = _arguments(sys.argv[1:] if argv is None else argv)
    try:
        style, source = resolve_docstring_style(
            args.docstring_style, os.environ, Path.cwd()
        )
    except AuditError as error:
        print(f"quality audit: {error}", file=sys.stderr)
        return 2
    if args.print_docstring_style:
        return _print_resolved_style(style, source, args.format)
    try:
        selection = collect_git_diff(Path.cwd()) if args.git_diff else None
        paths = selection.paths if selection else tuple(_iter_python_files(args.paths))
    except AuditError as error:
        print(f"quality audit: {error}", file=sys.stderr)
        return 2
    findings = _audit_paths(paths, style)
    if selection:
        findings = filter_changed_findings(findings, selection.changed_lines)
    findings.sort(key=lambda item: (item.path, item.line, item.code))
    _print_json(findings) if args.format == "json" else _print_text(findings)
    if args.fail_on == "none":
        return 0
    threshold = SEVERITY_RANK[args.fail_on]
    return 1 if any(SEVERITY_RANK[item.severity] >= threshold for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
