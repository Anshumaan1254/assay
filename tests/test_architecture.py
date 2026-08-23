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
# Nothing outside eval/ ever imports datagen (ground truth is quarantined).
#
# Deliberately dynamic rather than a hardcoded tuple of directories: a fixed
# (CORE_DIR, LLM_DIR, CLI_DIR) list previously let chaos/ (and any future
# top-level package) go unchecked. Enumerating every top-level package and
# excluding only eval/ and datagen/ itself means a new package can't
# silently slip through the way chaos/ did.
# ---------------------------------------------------------------------------


def _top_level_packages(root: Path, exclude: set[str]) -> list[Path]:
    """Every direct child of `root` that is a real Python package (has an
    __init__.py), minus `exclude`."""
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and child.name not in exclude and (child / "__init__.py").exists()
    )


def test_top_level_packages_includes_chaos_and_excludes_eval_and_datagen():
    # Regression guard for the bug this fix addresses: chaos/ (and any
    # future package) was previously un-checked because the old test
    # hardcoded (CORE_DIR, LLM_DIR, CLI_DIR).
    names = {p.name for p in _top_level_packages(REPO_ROOT, exclude={"eval", "datagen"})}
    assert names == {"core", "llm", "chaos", "cli"}


def test_everything_except_eval_and_datagen_never_imports_datagen():
    offenders = []
    for directory in _top_level_packages(REPO_ROOT, exclude={"eval", "datagen"}):
        for path in _py_files(directory):
            violations = guard_core.check_imports(_parse(path))
            for lineno, rule, message in violations:
                if rule == "forbidden-import" and "datagen" in message:
                    offenders.append(f"{path}:{lineno}: {message}")
    assert offenders == [], "\n".join(offenders)


def test_dynamic_package_enumeration_catches_a_future_new_package(tmp_path):
    # Proves the mechanism, not just today's package list: a brand-new
    # top-level package with a forbidden datagen import must be caught,
    # without the test file needing to be edited to know its name.
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "__init__.py").touch()
    (tmp_path / "datagen").mkdir()
    (tmp_path / "datagen" / "__init__.py").touch()
    new_pkg = tmp_path / "future_pkg"
    new_pkg.mkdir()
    (new_pkg / "__init__.py").touch()
    (new_pkg / "bad.py").write_text("from datagen.inject import apply_discrepancies\n", encoding="utf-8")

    offenders = []
    for directory in _top_level_packages(tmp_path, exclude={"eval", "datagen"}):
        for path in _py_files(directory):
            for lineno, rule, message in guard_core.check_imports(_parse(path)):
                if rule == "forbidden-import" and "datagen" in message:
                    offenders.append(f"{path}:{lineno}: {message}")
    assert len(offenders) == 1


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
