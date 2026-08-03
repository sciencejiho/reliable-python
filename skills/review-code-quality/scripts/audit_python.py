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
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


EXCLUDED_DIRECTORIES = {
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
}
MAX_AST_NODES = 100_000
MAX_CHAIN_DEPTH = 64
MUTABLE_BUILDERS = {"dict", "list", "set", "defaultdict", "OrderedDict"}
DYNAMIC_BUILTINS = {"__import__", "compile", "eval", "exec"}
REFLECTION_BUILTINS = {"delattr", "getattr", "setattr"}
MUTATING_METHODS = {"add", "append", "extend", "insert", "setdefault", "update"}
TERMINATORS = (ast.Break, ast.Continue, ast.Raise, ast.Return)
SUPPRESSION_TOKEN = "quality: ignore["
SUPPRESSION_RE = re.compile(
    r"#\s*quality:\s*ignore\[([A-Z][A-Z0-9]+)\]\s*-\s*(\S.*)$"
)
HUNK_RE = re.compile(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")
SEVERITY_RANK = {"note": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    line: int
    end_line: int
    message: str
    remedy: str


@dataclass(frozen=True)
class FunctionInfo:
    qualified_name: str
    class_name: str | None
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class DiffSelection:
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
            for token in tokens:
                if token.type != tokenize.COMMENT or SUPPRESSION_TOKEN not in token.string:
                    continue
                match = SUPPRESSION_RE.search(token.string)
                if match:
                    self.suppressions[token.start[0]].add(match.group(1))
                else:
                    self.invalid_suppressions.add(token.start[0])
        except (IndentationError, tokenize.TokenError):
            pass


def _run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *arguments],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_root(cwd: Path) -> Path:
    result = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise ValueError("--git-diff requires a Git working tree")
    return Path(result.stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()


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
        tracked = _decode_nul_paths(_run_git(root, [*diff_args, "HEAD", "--", "*.py"]).stdout)
    else:
        cached = _decode_nul_paths(
            _run_git(root, [*diff_args, "--cached", "--", "*.py"]).stdout
        )
        unstaged = _decode_nul_paths(_run_git(root, [*diff_args, "--", "*.py"]).stdout)
        tracked = cached | unstaged
    untracked = _decode_nul_paths(
        _run_git(root, ["ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"]).stdout
    )
    return tracked, untracked


def _parse_patch_ranges(payload: bytes, root: Path) -> dict[Path, list[tuple[int, int]]]:
    ranges: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    current_path: Path | None = None
    text = payload.decode("utf-8", errors="replace")
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
            ranges[current_path].append((start, start + count - 1))
    return ranges


def _patch_ranges(root: Path, has_head: bool) -> dict[Path, list[tuple[int, int]]]:
    args = ["diff", "--unified=0", "--no-color"]
    payloads: list[bytes]
    if has_head:
        payloads = [_run_git(root, [*args, "HEAD", "--", "*.py"]).stdout]
    else:
        payloads = [
            _run_git(root, [*args, "--cached", "--", "*.py"]).stdout,
            _run_git(root, [*args, "--", "*.py"]).stdout,
        ]
    combined: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    for payload in payloads:
        for path, items in _parse_patch_ranges(payload, root).items():
            combined[path].extend(items)
    return combined


def collect_git_diff(cwd: Path) -> DiffSelection:
    root = _git_root(cwd)
    has_head = _has_head(root)
    tracked, untracked = _changed_path_names(root, has_head)
    ranges = _patch_ranges(root, has_head)
    all_names = sorted(tracked | untracked)
    paths = tuple((root / name).resolve() for name in all_names if (root / name).is_file())
    for name in untracked:
        path = (root / name).resolve()
        if not path.is_file():
            continue
        line_count = max(1, len(path.read_text(encoding="utf-8").splitlines()))
        ranges[path] = [(1, line_count)]
    frozen_ranges = {path: tuple(items) for path, items in ranges.items()}
    return DiffSelection(root=root, paths=paths, changed_lines=frozen_ranges)


def _iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for supplied in paths:
        path = supplied.resolve()
        candidates = path.rglob("*.py") if path.is_dir() else (path,)
        for candidate in candidates:
            if candidate.suffix != ".py" or any(part in EXCLUDED_DIRECTORIES for part in candidate.parts):
                continue
            resolved = candidate.resolve()
            if resolved.is_file() and resolved not in seen:
                seen.add(resolved)
                yield resolved


def _node_end(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))


def _is_suppressed(lines: Sequence[str], line_number: int, code: str) -> bool:
    suppressions = getattr(lines, "suppressions", {})
    for candidate in (line_number, line_number - 1):
        if code in suppressions.get(candidate, ()):
            return True
    return False


def _append(
    findings: list[Finding],
    lines: Sequence[str],
    path: Path,
    code: str,
    severity: str,
    node: ast.AST,
    message: str,
    remedy: str,
) -> None:
    line = int(getattr(node, "lineno", 1))
    if _is_suppressed(lines, line, code):
        return
    findings.append(
        Finding(code, severity, str(path), line, _node_end(node), message, remedy)
    )


class FunctionCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.class_stack: list[str] = []
        self.functions: list[FunctionInfo] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        class_name = ".".join(self.class_stack) or None
        self.functions.append(FunctionInfo(qualified, class_name, node))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def _walk_without_nested_definitions(node: ast.AST) -> Iterator[ast.AST]:
    pending: deque[ast.AST] = deque(ast.iter_child_nodes(node))
    for _ in range(MAX_AST_NODES):
        if not pending:
            return
        current = pending.popleft()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield current
        pending.extend(ast.iter_child_nodes(current))


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
        return candidate if candidate in {item for values in names.values() for item in values} else None
    return None


def _recursive_functions(functions: Sequence[FunctionInfo]) -> set[str]:
    names: dict[str, list[str]] = defaultdict(list)
    for function in functions:
        names[function.node.name].append(function.qualified_name)
    graph: dict[str, set[str]] = {function.qualified_name: set() for function in functions}
    for function in functions:
        for node in _walk_without_nested_definitions(function.node):
            if isinstance(node, ast.Call):
                target = _resolve_local_call(node, function, names)
                if target in graph:
                    graph[function.qualified_name].add(target)
    recursive: set[str] = set()
    max_steps = max(1, len(graph) + 1)
    for origin in graph:
        pending = deque(graph[origin])
        visited: set[str] = set()
        for _ in range(max_steps):
            if not pending:
                break
            current = pending.popleft()
            if current == origin:
                recursive.add(origin)
                break
            if current in visited:
                continue
            visited.add(current)
            pending.extend(graph.get(current, ()))
    return recursive


def _numeric_constant(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    return None


def _has_monotonic_update(body: Sequence[ast.stmt], name: str, increasing: bool) -> bool:
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.AugAssign) or not isinstance(node.target, ast.Name):
                continue
            amount = _numeric_constant(node.value)
            if node.target.id != name or amount is None or amount <= 0:
                continue
            if increasing and isinstance(node.op, ast.Add):
                return True
            if not increasing and isinstance(node.op, ast.Sub):
                return True
    return False


def _while_is_statically_bounded(node: ast.While) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.left, ast.Name):
        return False
    bound = test.comparators[0]
    preset = _numeric_constant(bound) is not None or (
        isinstance(bound, ast.Name) and bound.id.isupper()
    )
    if not preset:
        return False
    if isinstance(test.ops[0], (ast.Lt, ast.LtE)):
        return _has_monotonic_update(node.body, test.left.id, increasing=True)
    if isinstance(test.ops[0], (ast.Gt, ast.GtE)):
        return _has_monotonic_update(node.body, test.left.id, increasing=False)
    return False


def _iterable_bound_kind(node: ast.AST) -> str | None:
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return "fixed"
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        upper = node.slice.upper
        capped = upper is not None and (
            _numeric_constant(upper) is not None
            or isinstance(upper, ast.Name) and upper.id.isupper()
        )
        return "truncating" if capped else None
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node.func)
    if name == "range" and node.args:
        bound = node.args[-1]
        fixed = _numeric_constant(bound) is not None or (
            isinstance(bound, ast.Name) and bound.id.isupper()
        )
        return "fixed" if fixed else None
    if name.endswith("islice") and len(node.args) >= 2:
        bound = node.args[-1]
        capped = _numeric_constant(bound) is not None or (
            isinstance(bound, ast.Name) and bound.id.isupper()
        )
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


def _invalid_suppressions(lines: Sequence[str], path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for number in sorted(getattr(lines, "invalid_suppressions", ())):
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
    return findings


def _check_functions(
    tree: ast.Module,
    functions: Sequence[FunctionInfo],
    lines: Sequence[str],
    path: Path,
    findings: list[Finding],
) -> None:
    recursive = _recursive_functions(functions)
    for function in functions:
        node = function.node
        if function.qualified_name in recursive:
            _append(findings, lines, path, "POT01", "error", node,
                    f"recursive call cycle includes {function.qualified_name}",
                    "Replace recursion with an explicitly bounded worklist.")
        span = _node_end(node) - node.lineno + 1
        if span > 60:
            _append(findings, lines, path, "POT04", "error", node,
                    f"function {function.qualified_name} spans {span} lines (limit: 60)",
                    "Extract coherent units without compressing statements.")
        positional = [*node.args.posonlyargs, *node.args.args]
        parameter_count = len(positional) + len(node.args.kwonlyargs)
        if positional and positional[0].arg in {"self", "cls"}:
            parameter_count -= 1
        if parameter_count > 5:
            _append(findings, lines, path, "CS04", "warning", node,
                    f"function {function.qualified_name} has {parameter_count} parameters",
                    "Introduce a cohesive parameter object or preserve an existing domain object.")
        defaults = [*node.args.defaults, *node.args.kw_defaults]
        if any(default is not None and _mutable_default(default) for default in defaults):
            _append(findings, lines, path, "POT06", "error", node,
                    f"function {function.qualified_name} has a mutable default argument",
                    "Use None and allocate the value inside the function.")
    if functions:
        total_checks = sum(_meaningful_check_count(function) for function in functions)
        density = total_checks / len(functions)
        if density < 2:
            first = functions[0].node
            _append(findings, lines, path, "POT05", "warning", first,
                    f"defensive-check density is {density:.2f} per function (target average: 2.00)",
                    "Add meaningful boundary checks or document why trivial functions need none.")


def _check_loops(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    for node in ast.walk(tree):
        bounded = True
        if isinstance(node, ast.While):
            bounded = _while_is_statically_bounded(node)
            if not bounded:
                _append(findings, lines, path, "POT02", "error", node,
                        "while loop has no mechanically visible preset upper bound",
                        "Use a named maximum and fail explicitly when it is exhausted.")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound_kind = _iterable_bound_kind(node.iter) if isinstance(node, ast.For) else None
            bounded = bound_kind is not None
            if not bounded:
                _append(findings, lines, path, "POT02", "warning", node,
                        "iteration bound depends on runtime data",
                        "Cap the iterable with a named maximum or justify an intentional service loop.")
            elif bound_kind == "truncating":
                _append(findings, lines, path, "POT02", "warning", node,
                        "iteration is capped but may silently discard input beyond the limit",
                        "Reject oversized input or return an explicit truncation/error signal.")
        else:
            continue
        if bounded:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr in MUTATING_METHODS:
                    _append(findings, lines, path, "POT03", "warning", child,
                            f"{child.func.attr} grows state inside a loop without a proven bound",
                            "Bound the loop and resource, stream results, or use a bounded container.")


def _check_exceptions_and_dynamic_code(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            is_broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in {"BaseException", "Exception"}
            )
            pass_only = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if is_broad and pass_only:
                _append(findings, lines, path, "POT07", "error", node,
                        "broad exception handler silently discards the failure",
                        "Catch the expected exception and recover or propagate it with context.")
            elif node.type is None:
                _append(findings, lines, path, "POT07", "warning", node,
                        "bare except catches control-flow and process-level exceptions",
                        "Catch only the exceptions this boundary can handle.")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        is_builtin_name = isinstance(node.func, ast.Name) or name.startswith("builtins.")
        leaf = name.split(".")[-1]
        if is_builtin_name and leaf in DYNAMIC_BUILTINS:
            _append(findings, lines, path, "POT08", "error", node,
                    f"dynamic code operation {name} hides behavior from static analysis",
                    "Use explicit parsing, dispatch tables, imports, or ordinary functions.")
        elif is_builtin_name and leaf in REFLECTION_BUILTINS:
            _append(findings, lines, path, "POT08", "warning", node,
                    f"reflective operation {name} obscures the accessed interface",
                    "Prefer explicit typed access or justify the framework boundary.")


def _attribute_depth(node: ast.Attribute) -> int:
    depth = 1
    current = node.value
    for _ in range(MAX_CHAIN_DEPTH):
        if not isinstance(current, ast.Attribute):
            break
        depth += 1
        current = current.value
    return depth


def _check_indirection_and_privacy(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if isinstance(parents.get(node), ast.Attribute):
            continue
        depth = _attribute_depth(node)
        if depth >= 3:
            _append(findings, lines, path, "POT09", "warning", node,
                    f"attribute chain has {depth} levels of indirection",
                    "Add a named boundary/query or use intermediate values with clear ownership.")
        root = node.value
        if node.attr.startswith("_") and isinstance(root, ast.Name) and root.id not in {"self", "cls"}:
            _append(findings, lines, path, "CS20", "warning", node,
                    f"code reaches into private state {root.id}.{node.attr}",
                    "Expose a narrow operation or move the behavior to the data owner.")


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
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self":
                        names.add(target.attr)
    return names


def _is_forwarder(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = node.body
    if len(body) != 1 or node.name.startswith("__"):
        return False
    statement = body[0]
    value = statement.value if isinstance(statement, (ast.Expr, ast.Return)) else None
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)


def _check_classes_and_conditionals(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and not isinstance(getattr(node, "_quality_parent", None), ast.If):
            length = _if_chain_length(node)
            if length >= 4:
                _append(findings, lines, path, "CS08", "warning", node,
                        f"conditional dispatch has {length} branches",
                        "Centralize the closed dispatch or use a typed strategy when variants recur.")
        elif isinstance(node, ast.Match) and len(node.cases) >= 5:
            _append(findings, lines, path, "CS08", "warning", node,
                    f"match dispatch has {len(node.cases)} branches",
                    "Keep one exhaustive closed-domain dispatch or move recurring behavior to variants.")
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        span = _node_end(node) - node.lineno + 1
        if span > 300 or len(methods) > 20:
            _append(findings, lines, path, "CS02", "warning", node,
                    f"class {node.name} spans {span} lines and {len(methods)} methods",
                    "Split only along demonstrated responsibility or change boundaries.")
        public_methods = [method for method in methods if not method.name.startswith("_")]
        assignments = _class_assignments(node)
        if len(assignments) >= 4 and len(public_methods) <= 1:
            _append(findings, lines, path, "CS15", "warning", node,
                    f"class {node.name} mainly exposes {len(assignments)} data fields",
                    "Move relevant invariants/behavior here, unless this is an intentional DTO or schema.")
        forwarders = [method for method in public_methods if _is_forwarder(method)]
        if len(forwarders) >= 3 and len(forwarders) * 2 >= len(public_methods):
            _append(findings, lines, path, "CS23", "warning", node,
                    f"class {node.name} mostly forwards {len(forwarders)} public methods",
                    "Remove the middle layer unless it protects a real policy or compatibility boundary.")


def _annotate_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "_quality_parent", parent)


def _check_dead_code(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    for parent in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(parent, field, None)
            if not isinstance(body, list):
                continue
            terminated = False
            for statement in body:
                if terminated:
                    _append(findings, lines, path, "CS16", "warning", statement,
                            "statement is unreachable after an unconditional terminator",
                            "Delete the dead statement after confirming no generated/reflection entry point.")
                    break
                terminated = isinstance(statement, TERMINATORS)


def _check_data_clumps(
    functions: Sequence[FunctionInfo], lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    groups: dict[tuple[str, ...], list[FunctionInfo]] = defaultdict(list)
    for function in functions:
        parameters = [
            arg.arg
            for arg in [*function.node.args.posonlyargs, *function.node.args.args, *function.node.args.kwonlyargs]
            if arg.arg not in {"self", "cls"}
        ]
        if len(parameters) >= 3:
            groups[tuple(parameters[:3])].append(function)
    for names, owners in groups.items():
        if len(owners) < 3:
            continue
        node = owners[0].node
        _append(findings, lines, path, "CS05", "warning", node,
                f"parameters {', '.join(names)} recur together in {len(owners)} functions",
                "Introduce a domain value only if the values share invariants and change together.")


def _check_duplicate_bodies(
    functions: Sequence[FunctionInfo], lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    seen: dict[str, FunctionInfo] = {}
    for function in functions:
        if len(function.node.body) < 2:
            continue
        fingerprint = ast.dump(ast.Module(body=function.node.body, type_ignores=[]), include_attributes=False)
        previous = seen.get(fingerprint)
        if previous is None:
            seen[fingerprint] = function
            continue
        _append(findings, lines, path, "CS14", "warning", function.node,
                f"function body duplicates {previous.qualified_name}",
                "Extract shared knowledge if both copies have the same change reason.")


def _check_module_scope(
    tree: ast.Module, lines: Sequence[str], path: Path, findings: list[Finding]
) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            _append(findings, lines, path, "POT06", "error", node,
                    f"global statement widens mutation scope for {', '.join(node.names)}",
                    "Return the value, pass explicit state, or encapsulate ownership.")
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not _mutable_default(value):
            continue
        _append(findings, lines, path, "POT06", "warning", node,
                "module-level mutable state has process-wide scope",
                "Use immutable configuration or encapsulate the state behind an explicit owner.")


def analyze_source(source: str, path: Path) -> list[Finding]:
    lines = SourceLines(source)
    findings = _invalid_suppressions(lines, path)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        line = error.lineno or 1
        findings.append(
            Finding("PARSE001", "error", str(path), line, line,
                    f"Python syntax error: {error.msg}", "Fix syntax before quality review.")
        )
        return findings
    _annotate_parents(tree)
    collector = FunctionCollector()
    collector.visit(tree)
    _check_functions(tree, collector.functions, lines, path, findings)
    _check_loops(tree, lines, path, findings)
    _check_exceptions_and_dynamic_code(tree, lines, path, findings)
    _check_indirection_and_privacy(tree, lines, path, findings)
    _check_classes_and_conditionals(tree, lines, path, findings)
    _check_dead_code(tree, lines, path, findings)
    _check_data_clumps(collector.functions, lines, path, findings)
    _check_duplicate_bodies(collector.functions, lines, path, findings)
    _check_module_scope(tree, lines, path, findings)
    return sorted(set(findings), key=lambda item: (item.line, item.code, item.message))


def analyze_path(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [
            Finding("IO001", "error", str(path), 1, 1,
                    f"cannot read Python source: {error}", "Make the file readable UTF-8 source.")
        ]
    return analyze_source(source, path)


def _intersects_changed_lines(finding: Finding, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(finding.line <= end and finding.end_line >= start for start, end in ranges)


def filter_changed_findings(
    findings: Iterable[Finding], changed_lines: dict[Path, tuple[tuple[int, int], ...]]
) -> list[Finding]:
    result: list[Finding] = []
    for finding in findings:
        ranges = changed_lines.get(Path(finding.path).resolve(), ())
        if ranges and _intersects_changed_lines(finding, ranges):
            result.append(finding)
    return result


def _summary(findings: Sequence[Finding]) -> dict[str, int]:
    counts = Counter(finding.severity for finding in findings)
    return {name: counts.get(name, 0) for name in ("error", "warning", "note")}


def _print_text(findings: Sequence[Finding]) -> None:
    if not findings:
        print("quality audit: no findings in the selected Python scope")
        return
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.severity} {finding.code}: "
            f"{finding.message}\n  remedy: {finding.remedy}"
        )
    counts = _summary(findings)
    print(f"quality audit: {counts['error']} error(s), {counts['warning']} warning(s)")


def _print_json(findings: Sequence[Finding]) -> None:
    print(json.dumps({"summary": _summary(findings), "findings": [asdict(item) for item in findings]}))


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Python files or directories to audit")
    parser.add_argument("--git-diff", action="store_true", help="audit changed Python lines in Git")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=("none", "error", "warning"), default="warning")
    args = parser.parse_args(argv)
    if args.git_diff and args.paths:
        parser.error("use --git-diff or explicit paths, not both")
    if not args.git_diff and not args.paths:
        parser.error("provide --git-diff or at least one path")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    try:
        selection = collect_git_diff(Path.cwd()) if args.git_diff else None
    except ValueError as error:
        print(f"quality audit: {error}", file=sys.stderr)
        return 2
    paths = selection.paths if selection else tuple(_iter_python_files(args.paths))
    findings = [finding for path in paths for finding in analyze_path(path)]
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
