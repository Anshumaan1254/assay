"""C10 -- float contamination.

Both real guards here (tests/test_architecture.py's exhaustive AST walk
over every file under core/, and scripts/guard_core.py's narrower
live-editing hook) are static -- source analysis, not a runtime check. A
true fault-injection-at-runtime test can't inject a float into
already-analyzed, already-passing source. So the chaos here is: write a
small fixture file with a deliberate float-on-Money violation and prove
the exact tool CLAUDE.md relies on (`scripts/guard_core.py`, wired into
`make guard` and a PostToolUse hook) catches it rather than silently
letting it merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

from chaos.incident import chaos_scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import guard_core

VIOLATION_SOURCE = '''\
def compute_fee(total: "Money", rate: float) -> "Money":
    mixed = total + rate
    with_call = total + float(2)
    halved = total / 2
    return mixed, with_call, halved
'''


def test_c10_float_arithmetic_on_a_money_value_is_caught_by_the_guard(tmp_path):
    with chaos_scenario(
        "C10",
        title="Float contamination",
        category="crash",
        failure_injected=(
            "a fixture .py file under core/ mixes float arithmetic with a Money-annotated variable"
        ),
        expected_behavior="scripts/guard_core.py flags every violation and its CLI exits non-zero",
    ) as scenario:
        fixture_dir = tmp_path / "core"
        fixture_dir.mkdir()
        fixture_path = fixture_dir / "bad_fee.py"
        fixture_path.write_text(VIOLATION_SOURCE, encoding="utf-8")

        violations = guard_core.check_file(fixture_path)

        assert violations, "the guard must catch this, not silently pass it"
        rules = {rule for _lineno, rule, _message in violations}
        assert "money-float-binop" in rules
        assert "money-div" in rules

        exit_code = guard_core.run(["guard_core.py", str(fixture_path)])
        assert exit_code == 1, "the CLI entry point itself must fail on this file"

        scenario.note(f"{len(violations)} violation(s): {sorted(rules)}")
