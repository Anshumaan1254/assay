"""Tests for `assay report` -- cli/report.py's read-back path.

Runs a real `assay audit` once via CliRunner to produce a genuine
report.json + AuditRunRow, then exercises `assay report` against it -- the
same "go through the real Typer app" discipline as tests/test_cli_app.py.
"""

from __future__ import annotations

import json
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


@pytest.fixture
def audited_run(clean_run_dir, tmp_path, monkeypatch) -> tuple[str, Path]:
    """Runs a real `assay audit` and returns (audit_run_id, store db path)."""
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    store_path = tmp_path / "store.db"
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output

    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )
    return run_id, store_path


def test_report_json_reads_back_the_persisted_report(audited_run, clean_run_dir, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["report", run_id, "--format", "json"])

    assert result.exit_code == 0, result.output
    on_disk = (clean_run_dir / "report.json").read_text(encoding="utf-8")
    assert json.loads(result.output) == json.loads(on_disk)


def test_report_table_includes_the_hash_and_cluster_lines(audited_run, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["report", run_id])  # default format: table

    assert result.exit_code == 0, result.output
    assert "audit run:" in result.output
    assert "report hash:" in result.output


def test_report_html_is_well_formed_and_escapes_free_text(audited_run, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["report", run_id, "--format", "html"])

    assert result.exit_code == 0, result.output
    assert "<!doctype html>" in result.output
    assert "<h1>" in result.output
    assert "<script" not in result.output


def test_report_refuses_cleanly_on_an_unknown_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "empty.db"))

    result = runner.invoke(cli.app, ["report", "AUD-does-not-exist"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output


def test_report_refuses_cleanly_when_the_artifact_is_missing(audited_run, clean_run_dir, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))
    (clean_run_dir / "report.json").unlink()

    result = runner.invoke(cli.app, ["report", run_id])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
