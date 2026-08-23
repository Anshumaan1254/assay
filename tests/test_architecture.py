"""Architectural invariants from CLAUDE.md, enforced as tests (not just the
PostToolUse guard hook, which only checks files as they're edited).

Reuses guard_core's AST-based import check rather than reimplementing it.
The float ban here is deliberately broader than guard_core's: guard_core
only flags a float co-occurring with a Money-annotated variable (a fast,
targeted signal for the live hook); this test enforces the actual invariant
— "floats are forbidden everywhere under core/" — with no exceptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import guard_core

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "core"
LLM_DIR = REPO_ROOT / "llm"
CLI_DIR = REPO_ROOT / "cli"


def _py_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# core/ never imports llm, datagen, anthropic, openai, or *_llm*
# ---------------------------------------------------------------------------


def test_core_never_imports_llm_or_datagen_or_llm_sdks():
    offenders = []
    for path in _py_files(CORE_DIR):
        violations = guard_core.check_imports(_parse(path))
        for lineno, rule, message in violations:
            if rule == "forbidden-import":
                offenders.append(f"{path}:{lineno}: {message}")
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# core/, llm/, cli/ never import datagen (ground truth is quarantined)
# ---------------------------------------------------------------------------


def test_core_llm_cli_never_import_datagen():
    offenders = []
    for directory in (CORE_DIR, LLM_DIR, CLI_DIR):
        for path in _py_files(directory):
            violations = guard_core.check_imports(_parse(path))
            for lineno, rule, message in violations:
                if rule == "forbidden-import" and "datagen" in message:
                    offenders.append(f"{path}:{lineno}: {message}")
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# core/ has zero float literals or float() calls, anywhere
# ---------------------------------------------------------------------------


def _find_floats(tree: ast.AST) -> list[int]:
    linenos = []
    for node in ast.walk(tree):
        is_float_literal = isinstance(node, ast.Constant) and isinstance(node.value, float)
        is_float_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
        if is_float_literal or is_float_call:
            linenos.append(node.lineno)
    return linenos


def test_core_has_no_float_literals_or_float_calls():
    offenders = []
    for path in _py_files(CORE_DIR):
        for lineno in _find_floats(_parse(path)):
            offenders.append(f"{path}:{lineno}")
    assert offenders == [], "\n".join(offenders)
