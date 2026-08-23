"""Pre-commit / post-edit guard for core/.

Enforces, by walking the AST (never regex), the invariants from
core/CLAUDE.md and the root CLAUDE.md:

  1. No imports of llm, datagen, anthropic, openai, or any module whose
     name matches *_llm* anywhere under core/.
  2. No float literal or float() call in the same statement as a
     variable annotated Money, and no binary op between a Money-
     annotated variable and a float (literal, float() call, or a
     variable annotated float).
  3. No round(), //, or / applied to a Money-annotated variable, except
     inside core/money.py itself.

Usage: python scripts/guard_core.py <path> [<path> ...]
Only .py files that live under a "core" directory are checked; anything
else on the argv list is silently ignored. Exits 1 with one line per
violation (file:line: [rule] message) if anything is found, else exits
0 with no output.
"""

from __future__ import annotations

import ast
import fnmatch
import sys
from pathlib import Path

FORBIDDEN_EXACT_MODULES = {"llm", "datagen", "anthropic", "openai"}
FORBIDDEN_GLOB = "*_llm*"

Violation = tuple[int, str, str]  # (lineno, rule_code, message)


def _module_component_forbidden(component: str) -> bool:
    return component in FORBIDDEN_EXACT_MODULES or fnmatch.fnmatch(component, FORBIDDEN_GLOB)


def check_imports(tree: ast.AST) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if any(_module_component_forbidden(p) for p in parts):
                    violations.append(
                        (node.lineno, "forbidden-import", f"import of '{alias.name}' is not allowed under core/")
                    )
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            candidates = list(parts)
            if node.module is None:
                # e.g. "from . import llm" -- the names ARE the modules
                candidates += [alias.name for alias in node.names]
            if any(_module_component_forbidden(p) for p in candidates):
                shown = node.module or ("." * node.level + ",".join(a.name for a in node.names))
                violations.append(
                    (node.lineno, "forbidden-import", f"import from '{shown}' is not allowed under core/")
                )
    return violations


def _annotation_names(annotation: ast.expr | None, target: str) -> bool:
    """True if `target` (e.g. "Money" or "float") appears anywhere in the annotation."""
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == target:
            return True
        if isinstance(node, ast.Attribute) and node.attr == target:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and target in node.value:
            return True
    return False


def _is_float_literal_or_call(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
        return True
    return False


class MoneyGuardVisitor(ast.NodeVisitor):
    """Scope-aware walk tracking which names are annotated Money / float."""

    STMT_TYPES_TO_SCAN = (
        ast.Assign,
        ast.AugAssign,
        ast.AnnAssign,
        ast.Return,
        ast.Expr,
        ast.If,
        ast.While,
        ast.Assert,
    )

    def __init__(self, enforce_div_round: bool):
        self.enforce_div_round = enforce_div_round
        self.violations: list[Violation] = []
        self._money_scopes: list[set[str]] = [set()]
        self._float_scopes: list[set[str]] = [set()]

    def _money_names(self) -> set[str]:
        names: set[str] = set()
        for scope in self._money_scopes:
            names |= scope
        return names

    def _float_names(self) -> set[str]:
        names: set[str] = set()
        for scope in self._float_scopes:
            names |= scope
        return names

    def _is_money_name(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id in self._money_names()

    def _is_floatish(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and node.id in self._float_names():
            return True
        return _is_float_literal_or_call(node)

    # -- scope handling ----------------------------------------------
    def _register_function_args(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            all_args.append(args.vararg)
        if args.kwarg:
            all_args.append(args.kwarg)
        for a in all_args:
            if _annotation_names(a.annotation, "Money"):
                self._money_scopes[-1].add(a.arg)
            if _annotation_names(a.annotation, "float"):
                self._float_scopes[-1].add(a.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._money_scopes.append(set())
        self._float_scopes.append(set())
        self._register_function_args(node)
        self.generic_visit(node)
        self._money_scopes.pop()
        self._float_scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            if _annotation_names(node.annotation, "Money"):
                self._money_scopes[-1].add(node.target.id)
            if _annotation_names(node.annotation, "float"):
                self._float_scopes[-1].add(node.target.id)
        self._check_statement(node)
        self.generic_visit(node)

    # -- statement-level float/Money co-occurrence --------------------
    def _check_statement(self, stmt: ast.stmt) -> None:
        exprs = [c for c in ast.iter_child_nodes(stmt) if isinstance(c, ast.expr)]
        money_names = self._money_names()
        has_money = False
        has_float = False
        for e in exprs:
            for sub in ast.walk(e):
                if isinstance(sub, ast.Name) and sub.id in money_names:
                    has_money = True
                if _is_float_literal_or_call(sub):
                    has_float = True
        if has_money and has_float:
            self.violations.append(
                (
                    stmt.lineno,
                    "money-float-mix",
                    "float literal or float() used in the same statement as a Money-annotated variable",
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_statement(node)
        if self.enforce_div_round and isinstance(node.op, (ast.Div, ast.FloorDiv)):
            if isinstance(node.target, ast.Name) and node.target.id in self._money_names():
                op = "/" if isinstance(node.op, ast.Div) else "//"
                self.violations.append(
                    (node.lineno, "money-div", f"use of '{op}=' on a Money value outside money.py")
                )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._check_statement(node)
        self.generic_visit(node)

    # -- binary ops: Money vs float, Money / and // --------------------
    def visit_BinOp(self, node: ast.BinOp) -> None:
        left_money = self._is_money_name(node.left)
        right_money = self._is_money_name(node.right)

        if (left_money and self._is_floatish(node.right)) or (right_money and self._is_floatish(node.left)):
            self.violations.append(
                (node.lineno, "money-float-binop", "binary operation between a Money value and a float")
            )

        if self.enforce_div_round and isinstance(node.op, (ast.Div, ast.FloorDiv)) and (left_money or right_money):
            op = "/" if isinstance(node.op, ast.Div) else "//"
            self.violations.append(
                (node.lineno, "money-div", f"use of '{op}' on a Money value outside money.py")
            )

        self.generic_visit(node)

    # -- round() on a Money value ---------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if self.enforce_div_round and isinstance(node.func, ast.Name) and node.func.id == "round":
            money_names = self._money_names()
            if any(isinstance(a, ast.Name) and a.id in money_names for a in node.args):
                self.violations.append(
                    (node.lineno, "money-round", "use of round() on a Money value outside money.py")
                )
        self.generic_visit(node)


def check_file(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        return [(0, "read-error", f"could not read file: {e}")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [(e.lineno or 0, "syntax-error", f"file has a syntax error and cannot be checked: {e.msg}")]

    violations = check_imports(tree)

    enforce_div_round = path.name != "money.py"
    visitor = MoneyGuardVisitor(enforce_div_round=enforce_div_round)
    visitor.visit(tree)
    violations.extend(visitor.violations)

    seen: set[Violation] = set()
    unique: list[Violation] = []
    for v in sorted(violations, key=lambda v: (v[0], v[1], v[2])):
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def _is_under_core(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return "core" in resolved.parts


def run(argv: list[str]) -> int:
    any_violations = False
    for raw in argv[1:]:
        path = Path(raw)
        if path.suffix != ".py":
            continue
        if not _is_under_core(path):
            continue
        if not path.is_file():
            continue
        for lineno, rule, message in check_file(path):
            any_violations = True
            print(f"{path}:{lineno}: [{rule}] {message}")
    return 1 if any_violations else 0


def main() -> None:
    sys.exit(run(sys.argv))


if __name__ == "__main__":
    main()
