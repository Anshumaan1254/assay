"""Tests for `assay chaos` -- cli/chaos.py.

Does NOT run the real chaos suite (real OS-level process kills, minutes to
complete -- see chaos/test_c02_kill_and_resume.py). Stubs run_and_stream and
asserts the two-step sequence and exit-code combination instead. The real
suite is verified manually once, by hand, per the working agreement.
"""

from __future__ import annotations

import sys

from typer.testing import CliRunner

import cli
import cli.chaos
from cli.chaos import run_chaos_suite

runner = CliRunner()


def test_runs_pytest_then_chaos_report_in_order(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run_and_stream(cmd: list[str]) -> int:
        calls.append(cmd)
        return 0

    monkeypatch.setattr(cli.chaos, "run_and_stream", _fake_run_and_stream)

    code = run_chaos_suite()

    assert code == 0
    assert calls == [
        [sys.executable, "-m", "pytest", "chaos/", "-q", "--timeout=600"],
        [sys.executable, "scripts/chaos_report.py"],
    ]


def test_chaos_report_still_runs_when_the_suite_fails(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run_and_stream(cmd: list[str]) -> int:
        calls.append(cmd)
        return 1 if "pytest" in cmd else 0

    monkeypatch.setattr(cli.chaos, "run_and_stream", _fake_run_and_stream)

    code = run_chaos_suite()

    assert code != 0
    assert len(calls) == 2, "scripts/chaos_report.py must still run even when the suite failed"


def test_nonzero_exit_if_either_step_fails(monkeypatch):
    for suite_code, report_code in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        def _fake_run_and_stream(cmd, _s=suite_code, _r=report_code):
            return _s if "pytest" in cmd else _r

        monkeypatch.setattr(cli.chaos, "run_and_stream", _fake_run_and_stream)
        code = run_chaos_suite()
        expected_nonzero = bool(suite_code or report_code)
        assert bool(code) == expected_nonzero, (suite_code, report_code, code)


def test_assay_chaos_command_propagates_a_failure_through_the_real_typer_app(monkeypatch):
    monkeypatch.setattr(cli.chaos, "run_and_stream", lambda cmd: 1)

    result = runner.invoke(cli.app, ["chaos"])

    assert result.exit_code == 1


def test_assay_chaos_command_exits_zero_on_success_through_the_real_typer_app(monkeypatch):
    monkeypatch.setattr(cli.chaos, "run_and_stream", lambda cmd: 0)

    result = runner.invoke(cli.app, ["chaos"])

    assert result.exit_code == 0
