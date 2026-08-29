"""Tests for `assay demo` -- cli/demo.py.

Does NOT run the real demo (generates a settlement month, audits 17,707
records, scores an eval run -- ~30 seconds and a lot of disk). Stubs
run_and_stream and asserts the sequence, the short-circuit and the exit
code instead. The real demo is verified by hand, per the working
agreement, and by the Docker job in CI.
"""

from __future__ import annotations

import sys

from typer.testing import CliRunner

import cli
import cli.demo
from cli.demo import DEMO_STEPS, run_demo

runner = CliRunner()


def test_runs_the_four_steps_in_order(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run_and_stream(cmd: list[str]) -> int:
        calls.append(cmd)
        return 0

    monkeypatch.setattr(cli.demo, "run_and_stream", _fake_run_and_stream)

    code = run_demo()

    assert code == 0
    assert calls == [
        [sys.executable, "-m", "cli", "generate", "--profile", "realistic", "--seed", "42"],
        [sys.executable, "-m", "cli", "contract", "compile", "runs/realistic-seed42/rate_card.md"],
        [sys.executable, "-m", "cli", "audit", "--run-dir", "runs/realistic-seed42"],
        [
            sys.executable, "-m", "cli", "eval", "--profiles", "realistic", "--seeds", "42",
            "--no-ablate", "--no-diagrams", "--out", "eval/results/demo",
        ],
    ]


def test_the_run_dir_is_relative_so_report_hash_does_not_depend_on_the_checkout_path():
    """`run_dir` is inside the report's hash payload, so an absolute path
    makes report_hash a function of where the repo sits on disk -- the
    DECISIONS.md 2026-08-30 00:20 incident, which reached production once
    already through scripts/vercel_reviewer_build.py."""
    audit_step = next(step for step in DEMO_STEPS if step[0] == "audit")
    run_dir = audit_step[audit_step.index("--run-dir") + 1]

    assert run_dir == "runs/realistic-seed42"
    assert not run_dir.startswith(("/", "\\")) and ":" not in run_dir
    assert "\\" not in run_dir, "a backslash separator hashes differently from a forward slash"


def test_stops_at_the_first_failing_step(monkeypatch):
    """Unlike `assay chaos`, which runs its reporter even after a failure:
    each demo step consumes what the previous one wrote, so continuing
    past a failure reports a confusing downstream error, not the real one."""
    calls: list[list[str]] = []

    def _fake_run_and_stream(cmd: list[str]) -> int:
        calls.append(cmd)
        return 1 if "contract" in cmd else 0

    monkeypatch.setattr(cli.demo, "run_and_stream", _fake_run_and_stream)

    code = run_demo()

    assert code == 1
    assert len(calls) == 2, "audit and eval must not run after contract compile failed"


def test_propagates_the_failing_step_exit_code(monkeypatch):
    for failing_index in range(len(DEMO_STEPS)):
        def _fake_run_and_stream(cmd, _i=failing_index, _seen=[]):  # noqa: B006
            _seen.append(cmd)
            return 3 if len(_seen) == _i + 1 else 0

        monkeypatch.setattr(cli.demo, "run_and_stream", _fake_run_and_stream)
        assert run_demo() == 3, f"step {failing_index} failing must surface as a nonzero code"


def test_assay_demo_command_propagates_a_failure_through_the_real_typer_app(monkeypatch):
    monkeypatch.setattr(cli.demo, "run_and_stream", lambda cmd: 1)

    result = runner.invoke(cli.app, ["demo"])

    assert result.exit_code == 1


def test_assay_demo_command_exits_zero_on_success_through_the_real_typer_app(monkeypatch):
    monkeypatch.setattr(cli.demo, "run_and_stream", lambda cmd: 0)

    result = runner.invoke(cli.app, ["demo"])

    assert result.exit_code == 0
