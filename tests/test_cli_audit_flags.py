"""Tests for the `--batch` alias and `--contract` pin on `assay audit` --
cli/__init__.py::audit(). Money-touching (report_hash, which contract gets
recomputed against), so written test-first per the working agreement.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest
from typer.testing import CliRunner

import cli
import cli.contract
from cli.contract import compile_ratecard_file
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


class _RefusesContractParseProvider:
    """Answers everything except a rate-card parse -- proves `--contract`
    genuinely skips recompilation rather than happening to hit a cache."""

    def __init__(self) -> None:
        self._cached = _offline_provider()

    def generate_structured(self, prompt: str, schema, model_hint: str):
        from core.contract import RateCardParse

        if schema is RateCardParse:
            raise AssertionError("assay audit --contract must not recompile the rate card")
        return self._cached.generate_structured(prompt, schema, model_hint)


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


def test_batch_is_a_true_alias_for_run_dir(clean_run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_provider", _offline_provider)

    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "a.db"))
    via_run_dir = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir), "--no-adjudicate"])
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "b.db"))
    via_batch = runner.invoke(cli.app, ["audit", "--batch", str(clean_run_dir), "--no-adjudicate"])

    assert via_run_dir.exit_code == 0, via_run_dir.output
    assert via_batch.exit_code == 0, via_batch.output

    def _hash(output: str) -> str:
        return next(line for line in output.splitlines() if line.startswith("report hash:"))

    assert _hash(via_run_dir.output) == _hash(via_batch.output)


def test_contract_flag_pins_a_precompiled_contract_and_skips_recompilation(clean_run_dir, tmp_path, monkeypatch):
    contract, contract_path = compile_ratecard_file(
        clean_run_dir / "rate_card.md", _offline_provider(), out=tmp_path / "pinned.contract.json"
    )

    monkeypatch.setattr(cli, "default_provider", lambda: _RefusesContractParseProvider())
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))

    result = runner.invoke(
        cli.app,
        ["audit", "--run-dir", str(clean_run_dir), "--contract", str(contract_path), "--no-adjudicate"],
    )

    assert result.exit_code == 0, result.output
    assert contract.version_id in result.output


def test_contract_flag_refuses_a_mismatched_pin(clean_run_dir, tmp_path, monkeypatch):
    from core.contract import CompiledContract, RateCardParse

    # An arbitrary, trivially-valid (zero rules) contract whose recorded
    # source_sha256 cannot possibly match clean_run_dir's real rate_card.md
    # -- no LLM call needed, since the mismatch check only ever compares
    # hashes and never re-parses either side.
    mismatched = CompiledContract.from_parse(
        RateCardParse(merchant_id="MERCH-OTHER", rules=[]), source_sha256="0" * 64
    )
    contract_path = tmp_path / "mismatched.contract.json"
    mismatched.save(contract_path)

    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store2.db"))

    result = runner.invoke(
        cli.app,
        ["audit", "--run-dir", str(clean_run_dir), "--contract", str(contract_path), "--no-adjudicate"],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
