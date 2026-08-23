"""Tests for scripts/guard_core.py.

Covers the guard's three enforced rules (forbidden imports, Money/float
mixing, round()///  // on Money outside money.py), its core/-only scoping,
and its CLI behavior (exit codes, output format, speed).
"""

import subprocess
import sys
import time
from pathlib import Path

import guard_core

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = REPO_ROOT / "scripts" / "guard_core.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run_guard(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
    )


def rule_codes(violations) -> set[str]:
    return {rule for _lineno, rule, _msg in violations}


def check_source(tmp_path, source: str, filename: str = "sample.py", under_core: bool = True):
    directory = tmp_path / "core" if under_core else tmp_path / "elsewhere"
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / filename
    file_path.write_text(source, encoding="utf-8")
    return guard_core.check_file(file_path), file_path


# ---------------------------------------------------------------------------
# Rule 1: forbidden imports
# ---------------------------------------------------------------------------


def test_flags_import_of_llm_module(tmp_path):
    violations, _ = check_source(tmp_path, "import llm\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_import_of_llm_submodule(tmp_path):
    violations, _ = check_source(tmp_path, "import llm.contract_parser\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_from_llm_import(tmp_path):
    violations, _ = check_source(tmp_path, "from llm.provider import LLMProvider\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_from_datagen_import(tmp_path):
    violations, _ = check_source(tmp_path, "from datagen.generator import plant_discrepancy\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_relative_import_of_llm(tmp_path):
    violations, _ = check_source(tmp_path, "from . import llm\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_anthropic_and_openai(tmp_path):
    violations, _ = check_source(tmp_path, "import anthropic\nimport openai\n")
    assert len(violations) == 2
    assert rule_codes(violations) == {"forbidden-import"}


def test_flags_module_matching_llm_glob(tmp_path):
    violations, _ = check_source(tmp_path, "import my_llm_helper\n")
    assert rule_codes(violations) == {"forbidden-import"}


def test_does_not_flag_unrelated_imports(tmp_path):
    violations, _ = check_source(
        tmp_path,
        "import os\nfrom typing import List\nimport pathlib as pl\nfrom core.money import Money\n",
    )
    assert violations == []


def test_does_not_flag_lookalike_names(tmp_path):
    # "llmish" and "anthropic_sdk" are not exact matches and don't contain "_llm".
    violations, _ = check_source(tmp_path, "import llmish\nimport anthropic_sdk\n")
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 2: float mixed with a Money-annotated variable
# ---------------------------------------------------------------------------


def test_flags_binop_between_money_param_and_float_literal(tmp_path):
    src = "def f(total: Money, rate: float) -> Money:\n    return total + rate\n"
    violations, _ = check_source(tmp_path, src)
    assert "money-float-binop" in rule_codes(violations)


def test_flags_binop_between_money_and_float_call(tmp_path):
    src = "def f(total: Money) -> Money:\n    return total + float(2)\n"
    violations, _ = check_source(tmp_path, src)
    assert "money-float-binop" in rule_codes(violations)
    assert "money-float-mix" in rule_codes(violations)


def test_flags_float_literal_in_call_alongside_money_var(tmp_path):
    src = "def f(total: Money) -> Money:\n    log_amount(total, 1.5)\n    return total\n"
    violations, _ = check_source(tmp_path, src)
    assert "money-float-mix" in rule_codes(violations)


def test_flags_annassign_money_var_mixed_with_float(tmp_path):
    src = "x: Money = Money.zero()\ny = x + 3.14\n"
    violations, _ = check_source(tmp_path, src)
    assert "money-float-binop" in rule_codes(violations)


def test_does_not_flag_money_with_int(tmp_path):
    src = "def f(total: Money, count: int) -> Money:\n    return total.multiply(count)\n"
    violations, _ = check_source(tmp_path, src)
    assert violations == []


def test_does_not_flag_money_with_money(tmp_path):
    src = "def f(a: Money, b: Money) -> Money:\n    return a.add(b)\n"
    violations, _ = check_source(tmp_path, src)
    assert violations == []


def test_does_not_flag_float_unrelated_to_money(tmp_path):
    src = "def f(rate: float) -> float:\n    return rate + 1.5\n"
    violations, _ = check_source(tmp_path, src)
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 3: round() / // / / on Money, outside money.py
# ---------------------------------------------------------------------------


def test_flags_round_on_money(tmp_path):
    src = "def f(total: Money):\n    return round(total)\n"
    violations, _ = check_source(tmp_path, src)
    assert rule_codes(violations) == {"money-round"}


def test_flags_true_division_on_money(tmp_path):
    src = "def f(total: Money):\n    return total / 2\n"
    violations, _ = check_source(tmp_path, src)
    assert rule_codes(violations) == {"money-div"}
    assert any("/" in msg and "//" not in msg for _, _, msg in violations)


def test_flags_floor_division_on_money(tmp_path):
    src = "def f(total: Money):\n    return total // 2\n"
    violations, _ = check_source(tmp_path, src)
    assert rule_codes(violations) == {"money-div"}


def test_flags_floor_division_augassign_on_money(tmp_path):
    src = "def f(total: Money):\n    total //= 2\n    return total\n"
    violations, _ = check_source(tmp_path, src)
    assert "money-div" in rule_codes(violations)


def test_does_not_flag_round_or_division_in_money_py(tmp_path):
    src = "def split(amount: Money, parts: int) -> Money:\n    share = amount // parts\n    return round(amount)\n"
    violations, _ = check_source(tmp_path, src, filename="money.py")
    assert rule_codes(violations) == set()


def test_does_not_flag_round_on_non_money_value(tmp_path):
    src = "def f(count: int):\n    return round(count / 3)\n"
    violations, _ = check_source(tmp_path, src)
    assert violations == []


def test_does_not_flag_division_between_plain_ints(tmp_path):
    src = "def f(a: int, b: int) -> int:\n    return a // b\n"
    violations, _ = check_source(tmp_path, src)
    assert violations == []


# ---------------------------------------------------------------------------
# Fixture files (static, checked into the repo)
# ---------------------------------------------------------------------------


def test_violations_fixture_trips_every_rule():
    violations = guard_core.check_file(FIXTURES / "core" / "violations.py")
    codes = rule_codes(violations)
    assert codes == {
        "forbidden-import",
        "money-float-binop",
        "money-float-mix",
        "money-div",
        "money-round",
    }


def test_clean_fixture_has_no_violations():
    violations = guard_core.check_file(FIXTURES / "core" / "clean.py")
    assert violations == []


def test_money_py_fixture_exempt_from_div_and_round():
    violations = guard_core.check_file(FIXTURES / "core" / "money.py")
    assert violations == []


def test_syntax_error_is_reported_not_raised(tmp_path):
    path = tmp_path / "core" / "broken.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def f(:\n    pass\n", encoding="utf-8")
    violations = guard_core.check_file(path)
    assert rule_codes(violations) == {"syntax-error"}


# ---------------------------------------------------------------------------
# core/-only scoping
# ---------------------------------------------------------------------------


def test_is_under_core_true_for_core_path(tmp_path):
    assert guard_core._is_under_core(tmp_path / "core" / "money.py") is True


def test_is_under_core_false_for_other_path(tmp_path):
    assert guard_core._is_under_core(tmp_path / "llm" / "adjudicator.py") is False


def test_run_ignores_files_outside_core():
    exit_code = guard_core.run(["guard_core.py", str(FIXTURES / "not_core" / "violations.py")])
    assert exit_code == 0


def test_run_ignores_non_python_files(tmp_path):
    non_py = tmp_path / "core" / "notes.txt"
    non_py.parent.mkdir(parents=True, exist_ok=True)
    non_py.write_text("import llm\n", encoding="utf-8")
    assert guard_core.run(["guard_core.py", str(non_py)]) == 0


def test_run_ignores_missing_files(tmp_path):
    missing = tmp_path / "core" / "does_not_exist.py"
    assert guard_core.run(["guard_core.py", str(missing)]) == 0


# ---------------------------------------------------------------------------
# CLI: exit codes, output, and speed
# ---------------------------------------------------------------------------


def test_cli_exits_1_with_violation_lines_for_bad_file():
    result = run_guard(FIXTURES / "core" / "violations.py")
    assert result.returncode == 1
    assert str(FIXTURES / "core" / "violations.py") in result.stdout
    assert "[forbidden-import]" in result.stdout
    assert "[money-round]" in result.stdout


def test_cli_exits_0_silently_for_clean_file():
    result = run_guard(FIXTURES / "core" / "clean.py")
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_exits_0_silently_when_no_paths_given():
    result = run_guard()
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_reports_only_the_dirty_file_among_several():
    result = run_guard(FIXTURES / "core" / "clean.py", FIXTURES / "core" / "violations.py")
    assert result.returncode == 1
    assert "clean.py" not in result.stdout
    assert "violations.py" in result.stdout


def test_guard_runs_fast_enough_to_be_invisible():
    start = time.perf_counter()
    guard_core.check_file(FIXTURES / "core" / "violations.py")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2
