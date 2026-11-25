"""Enforce CS-1 through CS-3 and the dependency matrix in ADR-0002."""

import ast
import sys
import unittest
from graphlib import CycleError, TopologicalSorter
from importlib.util import resolve_name
from pathlib import Path
from typing import Iterator

PACKAGE = "agent_dispatch"
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / PACKAGE

L0_TO_L2 = {"config", "secrets", "process"}
L3 = {"brief", "linear", "git_ops", "harness"}
L0_TO_L3 = L0_TO_L2 | L3
L0_TO_L4 = L0_TO_L3 | {"gates", "review", "publish"}
PIPELINE_GROUP = {"pipeline", "preflight", "recovery"}
COMMANDS_GROUP = {"commands", "maintenance"}

ALLOWED_IMPORTS = {
    "config": set(),
    "secrets": {"config"},
    "process": {"config", "secrets"},
    **{module: L0_TO_L2 for module in L3},
    "gates": L0_TO_L2 | {"brief", "git_ops"},
    "review": L0_TO_L2 | {"brief", "harness", "gates"},
    "publish": L0_TO_L2 | {"linear", "git_ops", "harness", "review"},
    **{module: L0_TO_L4 | (PIPELINE_GROUP - {module}) for module in PIPELINE_GROUP},
    "scheduler": L0_TO_L3 | {"pipeline"},
    **{module: L0_TO_L4 | {"pipeline", "scheduler"} | (COMMANDS_GROUP - {module}) for module in COMMANDS_GROUP},
    "cli": L0_TO_L2 | {"commands"},
}


def imported_modules(node: ast.Import | ast.ImportFrom, package: str) -> Iterator[str]:
    if isinstance(node, ast.Import):
        yield from (name.name for name in node.names)
        return

    module = resolve_name("." * node.level + (node.module or ""), package)
    if module == PACKAGE:
        yield from (f"{PACKAGE}.{name.name}" for name in node.names)
    else:
        yield module


class TestArchitecture(unittest.TestCase):
    def test_cli_launcher_only_bootstraps_the_package_and_runs_main(self) -> None:
        launcher = REPO_ROOT / "bin" / "agent-dispatch"
        source = launcher.read_text(encoding="utf-8")
        lines = source.splitlines()
        self.assertLessEqual(len(lines), 40, "CS-3 launcher exceeds 40 lines")
        self.assertEqual(lines[:1], ["#!/usr/bin/env python3"])

        tree = ast.parse(source, filename=str(launcher))
        body = list(tree.body)
        if body and ast.get_docstring(tree) is not None:
            body.pop(0)

        top_imports: list[ast.stmt] = []
        main_guards: list[ast.If] = []
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                top_imports.append(stmt)
            elif (
                isinstance(stmt, ast.If)
                and not stmt.orelse
                and isinstance(stmt.test, ast.Compare)
                and isinstance(stmt.test.left, ast.Name)
                and stmt.test.left.id == "__name__"
                and len(stmt.test.ops) == 1
                and isinstance(stmt.test.ops[0], ast.Eq)
                and len(stmt.test.comparators) == 1
                and isinstance(stmt.test.comparators[0], ast.Constant)
                and stmt.test.comparators[0].value == "__main__"
            ):
                main_guards.append(stmt)
            else:
                self.fail(
                    f"bin/agent-dispatch:{stmt.lineno}: CS-2/CS-3 launcher may contain only stdlib imports "
                    "and an `if __name__ == '__main__':` guard (no top-level logic or re-exports)"
                )

        self.assertEqual(len(main_guards), 1, "bin/agent-dispatch must define exactly one __main__ guard")
        for stmt in top_imports:
            for imported in imported_modules(stmt, ""):
                root = imported.partition(".")[0]
                self.assertIn(root, sys.stdlib_module_names, f"bin/agent-dispatch: CS-1 non-stdlib top import {imported}")

        problems: list[str] = []
        delegates_to_cli_main = False
        calls_main = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                problems.append(f"bin/agent-dispatch:{node.lineno}: CS-3 launcher must not define {node.name}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for imported in imported_modules(node, ""):
                    root = imported.partition(".")[0]
                    if root == PACKAGE:
                        if (
                            isinstance(node, ast.ImportFrom)
                            and node.module == f"{PACKAGE}.cli"
                            and [a.name for a in node.names] == ["main"]
                        ):
                            delegates_to_cli_main = True
                        else:
                            problems.append(f"bin/agent-dispatch:{node.lineno}: CS-2 launcher may only import agent_dispatch.cli.main")
                    elif root not in sys.stdlib_module_names:
                        problems.append(f"bin/agent-dispatch:{node.lineno}: CS-1 third-party import {imported}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main":
                calls_main = True

        if not (delegates_to_cli_main and calls_main):
            problems.append("bin/agent-dispatch must import and invoke agent_dispatch.cli.main() inside __main__")
        self.assertFalse(problems, "\n" + "\n".join(problems))

    def test_tests_use_package_qualified_helpers_import(self) -> None:
        tests_dir = REPO_ROOT / "tests"
        problems: list[str] = []
        for path in sorted(tests_dir.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "helpers" or alias.name.startswith("helpers."):
                            problems.append(f"{relative}:{node.lineno}: CS-7 import tests.helpers instead of bare '{alias.name}'")
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mod = node.module or ""
                    if mod == "helpers" or mod.startswith("helpers."):
                        problems.append(f"{relative}:{node.lineno}: CS-7 use 'from tests.helpers import ...' instead of bare '{mod}'")
        self.assertFalse(problems, "\n" + "\n".join(problems))

    def test_legacy_test_monolith_is_absent(self) -> None:
        legacy = REPO_ROOT / "tests" / "test_agent_dispatch.py"
        self.assertFalse(legacy.exists() or legacy.is_symlink(), "The legacy test monolith must stay retired")

    def test_package_module_limits_and_dependencies(self) -> None:
        marker = PACKAGE_DIR / "__init__.py"
        self.assertTrue(marker.is_file(), f"Missing package marker {marker.name}")
        problems: list[str] = []
        graph: dict[str, list[str]] = {}

        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            relative = path.relative_to(PACKAGE_DIR)
            module = ".".join(relative.with_suffix("").parts)
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(relative))
            lines = len(source.splitlines())
            if lines > 600:
                problems.append(f"{relative}: CS-3 module has {lines} lines (maximum 600)")

            if path == marker:
                docstring = ast.get_docstring(tree)
                if len(tree.body) != (0 if docstring is None else 1):
                    problems.append(f"{relative}: CS-2 package marker may contain only a docstring")
                continue

            if module not in ALLOWED_IMPORTS:
                problems.append(f"{relative}: CS-2 module has no layer in ADR-0002")
            allowed = ALLOWED_IMPORTS.get(module, set())
            dependencies: set[str] = set()
            package = ".".join((PACKAGE, *relative.parts[:-1]))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                    lines = node.end_lineno - start + 1
                    if lines > 100:
                        problems.append(f"{relative}:{start}: CS-3 {node.name} has {lines} lines (maximum 100)")

                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                location = f"{relative}:{node.lineno}"
                try:
                    for imported in imported_modules(node, package):
                        root, _, dependency = imported.partition(".")
                        if root == PACKAGE:
                            if dependency in allowed:
                                dependencies.add(dependency)
                            else:
                                problems.append(f"{location}: CS-2 {module} may not import {imported}")
                        elif root not in sys.stdlib_module_names:
                            problems.append(f"{location}: CS-1 third-party import {imported}")
                except ImportError as exc:
                    problems.append(f"{location}: CS-2 {exc}")
            graph[module] = sorted(dependencies)

        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            problems.append("CS-2 circular imports: " + " -> ".join(exc.args[1]))
        self.assertFalse(problems, "\n" + "\n".join(problems))
