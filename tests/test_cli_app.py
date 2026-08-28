"""Smoke tests for the `assay audit` Typer command itself -- cli/__init__.py.

Every other audit-path test exercises run_audit()/resume_or_run() directly;
this file is the one place that goes through the actual Typer app, so a
real wiring mistake in cli/__init__.py (wrong function called, a refusal
that leaks a raw traceback instead of a clean message) has somewhere to be
caught. default_provider() is monkeypatched to an offline, cache-only
provider so this needs no GEMINI_API_KEY and no network.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest
from typer.testing import CliRunner

import cli
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

runner = CliRunner()


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


@pytest.fixture(scope="module")
def clean_run_dir(tmp_path_factory) -> Path:
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(42))
    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "sanity check on the fixture itself"

    out_dir = tmp_path_factory.mktemp("clean") / "clean-seed42"
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": "clean-seed42", "seed": 42, "profile": "clean"},
        out_dir=out_dir,
        merchant_id=config.merchant_id,
    )
    return out_dir


@pytest.mark.timeout(120)
def test_assay_audit_runs_end_to_end_through_the_real_typer_app(clean_run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir)])

    assert result.exit_code == 0, result.output
    assert "audit run:" in result.output
    assert "report hash:" in result.output
    assert (clean_run_dir / "report.json").is_file(), "assay report/replay locate a run via this file"


@pytest.mark.timeout(120)
def test_assay_audit_json_output_is_unchanged_by_writing_report_json(clean_run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir), "--json"])

    assert result.exit_code == 0, result.output
    assert "report:" not in result.output


@pytest.mark.timeout(120)
def test_a_refusal_exits_cleanly_with_a_message_not_a_raw_traceback(clean_run_dir, tmp_path, monkeypatch):
    def _boom():
        raise ValueError("simulated: no contract and no provider")

    monkeypatch.setattr(cli, "default_provider", _boom)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "simulated: no contract and no provider" in result.output
