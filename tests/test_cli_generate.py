"""Tests for `assay generate` -- cli/generate.py.

A real subprocess invocation of `python -m datagen.cli generate`, not a
mock: no LLM is involved in generation, so this is fast, and the whole
point of this command is that it is a faithful pass-through, which a
mocked subprocess call couldn't prove.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import guard_core
from typer.testing import CliRunner

import cli

runner = CliRunner()


def test_assay_generate_produces_a_run_directory(tmp_path):
    result = runner.invoke(
        cli.app,
        [
            "generate",
            "--profile",
            "clean",
            "--seed",
            "7",
            "--out",
            str(tmp_path / "runs"),
            "--truth-out",
            str(tmp_path / "truth.json"),
        ],
    )

    # run_and_stream inherits the real OS stdout fd (by design, so
    # datagen's own progress prints live), which bypasses CliRunner's
    # sys.stdout capture -- the file-existence assertions below are this
    # test's actual proof the wrapper worked end to end.
    assert result.exit_code == 0

    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    for name in ("ledger.json", "settlement_report.json", "bank_statement.json", "rate_card.md", "manifest.json"):
        assert (run_dirs[0] / name).is_file()
    assert (tmp_path / "truth.json").is_file()


def test_an_invalid_profile_propagates_datagens_own_failure_not_a_traceback(tmp_path):
    result = runner.invoke(
        cli.app,
        [
            "generate",
            "--profile",
            "not-a-real-profile-or-path",
            "--seed",
            "7",
            "--out",
            str(tmp_path / "runs"),
            "--truth-out",
            str(tmp_path / "truth.json"),
        ],
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_generate_shells_out_rather_than_importing_datagen():
    """cli/generate.py itself must contain zero AST-level `import datagen`
    -- tests/test_architecture.py already enforces this dynamically for
    every file under cli/, but pinning it here too documents *why* this
    file is shaped the way it is. AST-based, like guard_core itself,
    rather than a text search: this module's own docstring legitimately
    mentions "datagen" in prose without importing it."""
    path = Path(sys.modules["cli.generate"].__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = [v for v in guard_core.check_imports(tree) if "datagen" in v[2]]
    assert violations == []
