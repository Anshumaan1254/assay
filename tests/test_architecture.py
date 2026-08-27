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
    assert names == {"core", "llm", "chaos", "cli", "store"}


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
# cli/'s MODULE-LEVEL import graph never reaches datagen, transitively.
#
# The per-file check above proves no file under cli/ writes `import
# datagen`. It cannot see a path that goes cli -> eval -> datagen, and one
# genuinely exists at runtime: `assay eval` is a real command and eval/ is
# the package that reads ground truth. That command's import is written
# INSIDE the function body on purpose, so importing `cli` never pulls
# datagen into the process at all -- which is what this test pins.
#
# Deliberately import-graph-based rather than a grep: the property that
# matters is "importing the product's CLI cannot load the answers", and
# that is a statement about the graph, not about any one file's text.
# ---------------------------------------------------------------------------


def _module_level_imports(tree: ast.AST) -> set[str]:
    """Dotted module names imported at module scope only.

    Anything nested inside a function or class body is skipped: that is the
    whole distinction this test exists to measure, and treating a
    function-local import as equivalent would defeat it.

    Names are kept dotted (`eval.determinism`, not `eval`) because the walk
    below has to model what Python actually loads. Importing
    `eval.determinism` executes `eval/__init__.py` and that one module --
    it does NOT execute `eval/harness.py`. A package-granular walk would
    report a leak through a module that was never imported, and a guard
    test that cries wolf is a guard test somebody eventually deletes.
    """
    names: set[str] = set()
    nodes = tree.body if isinstance(tree, ast.Module) else []
    for node in nodes:
        # `if TYPE_CHECKING:` and similar module-scope conditionals still
        # count as module scope.
        candidates = ast.walk(node) if isinstance(node, ast.If) else [node]
        for inner in candidates:
            if isinstance(inner, ast.Import):
                names.update(alias.name for alias in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.level == 0 and inner.module:
                names.add(inner.module)
    return names


def _module_path(dotted: str, root: Path) -> Path | None:
    """The file `dotted` would load, if it is first-party."""
    as_module = root / Path(*dotted.split(".")).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = root / Path(*dotted.split(".")) / "__init__.py"
    return as_package if as_package.is_file() else None


def _packages_loaded_by(dotted: str, root: Path = REPO_ROOT) -> set[str]:
    """Every first-party package a `import <dotted>` would actually load,
    following module-level imports transitively.

    Importing `a.b` also executes `a/__init__.py`, so each dotted name
    contributes its own file and every parent package's `__init__.py`.
    """
    seen_modules: set[str] = set()
    packages: set[str] = set()
    frontier = [dotted]

    while frontier:
        name = frontier.pop()
        if name in seen_modules:
            continue
        seen_modules.add(name)
        packages.add(name.split(".")[0])

        # The parent packages Python initialises on the way down.
        parts = name.split(".")
        for depth in range(1, len(parts)):
            frontier.append(".".join(parts[:depth]))

        path = _module_path(name, root)
        if path is None:
            continue
        for imported in _module_level_imports(_parse(path)):
            if _module_path(imported, root) is not None:
                frontier.append(imported)
    return packages


def _cli_module_names() -> list[str]:
    return [
        ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts).removesuffix(".__init__")
        for path in _py_files(CLI_DIR)
    ]


def test_importing_the_product_cli_can_never_load_datagen():
    """Invariant 5's real content, as a statement about the import graph:
    loading the product's CLI must not be able to load the answers."""
    for module in _cli_module_names():
        loaded = _packages_loaded_by(module)
        assert "datagen" not in loaded, (
            f"importing {module} loads datagen/ through a module-level import chain "
            f"(packages loaded: {sorted(loaded)}). The `assay eval` command must import "
            "eval/ inside its function body, not at module scope -- invariant 5."
        )


def test_the_import_graph_walk_would_actually_catch_a_transitive_leak(tmp_path):
    """Proves the mechanism, not just today's answer: a module that reaches
    datagen only through an intermediate must still be caught."""
    (tmp_path / "datagen").mkdir()
    (tmp_path / "datagen" / "__init__.py").touch()
    (tmp_path / "datagen" / "inject.py").touch()
    (tmp_path / "middle").mkdir()
    (tmp_path / "middle" / "__init__.py").touch()
    (tmp_path / "middle" / "scorer.py").write_text(
        "from datagen.inject import apply_discrepancies\n", encoding="utf-8"
    )
    (tmp_path / "front").mkdir()
    (tmp_path / "front" / "__init__.py").write_text("from middle.scorer import x\n", encoding="utf-8")

    assert "datagen" in _packages_loaded_by("front", root=tmp_path)


def test_the_walk_does_not_blame_a_sibling_module_that_was_never_imported(tmp_path):
    """The precision half. `import pkg.safe` must not be reported as
    loading datagen just because `pkg.unsafe` sits next to it -- this is
    exactly cli/audit.py importing eval.determinism while eval.harness
    imports datagen."""
    (tmp_path / "datagen").mkdir()
    (tmp_path / "datagen" / "__init__.py").touch()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").touch()
    (tmp_path / "pkg" / "safe.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "unsafe.py").write_text("import datagen\n", encoding="utf-8")

    assert "datagen" not in _packages_loaded_by("pkg.safe", root=tmp_path)
    assert "datagen" in _packages_loaded_by("pkg.unsafe", root=tmp_path)


def test_the_eval_command_exists_and_imports_lazily():
    """The other half: the command really is registered, so the lazy
    import is a deliberate arrangement rather than the command having
    quietly been dropped."""
    source = (CLI_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "def eval(" in source, "the `assay eval` command is gone"
    assert "from eval.cli import run_eval" in source
    module_level = _module_level_imports(_parse(CLI_DIR / "__init__.py"))
    assert not any(name.split(".")[0] == "eval" for name in module_level)


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
