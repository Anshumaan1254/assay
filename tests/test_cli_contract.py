"""Tests for `assay contract compile` -- cli/contract.py.

Runs offline off the committed `.llm_cache/`, the same as every other CLI
test that needs a compiled contract: `runs/realistic-seed42/rate_card.md`'s
prompt is already a cache hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli
import cli.contract
from cli.contract import PinnedContractMismatch, compile_ratecard_file, load_pinned_contract
from core.contract import CompiledContract
from llm.contract_parser import ContractParseRejected
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent
RATECARD = REPO_ROOT / "runs" / "realistic-seed42" / "rate_card.md"


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


class _RejectingProvider:
    def generate_structured(self, prompt: str, schema, model_hint: str) -> dict:
        return {"merchant_id": "x", "currency": "INR", "rules": "not a list", "dormant_clauses": []}


def test_compile_ratecard_file_writes_and_returns_a_compiled_contract(tmp_path):
    out = tmp_path / "compiled.json"
    contract, target = compile_ratecard_file(RATECARD, _offline_provider(), out=out)

    assert target == out
    assert target.is_file()
    assert isinstance(contract, CompiledContract)
    reloaded = CompiledContract.load(out)
    assert reloaded.version_id == contract.version_id


def test_compile_ratecard_file_defaults_out_to_ratecard_dot_contract_dot_json(tmp_path):
    ratecard = tmp_path / "card.md"
    ratecard.write_text(RATECARD.read_text(encoding="utf-8"), encoding="utf-8")

    _contract, target = compile_ratecard_file(ratecard, _offline_provider())

    assert target == tmp_path / "card.contract.json"
    assert target.is_file()


def test_a_rejected_parse_raises_rather_than_writing_anything(tmp_path):
    out = tmp_path / "compiled.json"
    with pytest.raises(ContractParseRejected):
        compile_ratecard_file(RATECARD, _RejectingProvider(), out=out)
    assert not out.exists()


def test_load_pinned_contract_accepts_a_matching_pin(tmp_path):
    out = tmp_path / "compiled.json"
    compile_ratecard_file(RATECARD, _offline_provider(), out=out)

    loaded = load_pinned_contract(out, RATECARD.parent)
    assert loaded.source_sha256

    reloaded = CompiledContract.load(out)
    assert loaded.version_id == reloaded.version_id


def test_load_pinned_contract_refuses_a_mismatched_pin(tmp_path):
    out = tmp_path / "compiled.json"
    compile_ratecard_file(RATECARD, _offline_provider(), out=out)

    other_run_dir = tmp_path / "other_run"
    other_run_dir.mkdir()
    (other_run_dir / "rate_card.md").write_text("# a different rate card\n", encoding="utf-8")

    with pytest.raises(PinnedContractMismatch):
        load_pinned_contract(out, other_run_dir)


def test_assay_contract_compile_runs_end_to_end_through_the_real_typer_app(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.contract, "default_provider", _offline_provider)
    out = tmp_path / "compiled.json"

    result = runner.invoke(cli.app, ["contract", "compile", str(RATECARD), "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert "version:" in result.output
    assert "wrote:" in result.output
    assert out.is_file()


def test_a_rejected_parse_exits_cleanly_through_the_real_typer_app(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.contract, "default_provider", lambda: _RejectingProvider())
    out = tmp_path / "compiled.json"

    result = runner.invoke(cli.app, ["contract", "compile", str(RATECARD), "--out", str(out)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
    assert not out.exists()
