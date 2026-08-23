"""writer.py: exact file set written, and every record round-trips through
the real core.models Pydantic classes (extra="forbid") -- the strongest
available check that datagen's output actually satisfies the schema it
claims to, not just that it's internally consistent."""

from __future__ import annotations

import json
from random import Random

from core.models import (
    Adjustment,
    BankCredit,
    Chargeback,
    FeeLine,
    Payment,
    Refund,
    SettlementBatch,
    TaxLine,
)
from datagen.config import GenerationConfig, InjectionProfile
from datagen.ground_truth import GroundTruth
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_ground_truth, write_run

_CONFIG = GenerationConfig(month="2026-07", total_payments=400)
_RATE_CARD = default_rate_card(_CONFIG.month)


def _reported_world_and_ground_truth():
    true_world = build_true_world(_CONFIG, _RATE_CARD, Random(9))
    profile = InjectionProfile(D01=2, D09=1)
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, profile, Random(10))
    ground_truth = GroundTruth(
        run_id="test-run",
        seed=9,
        profile="test",
        generated_at="2026-07-01T00:00:00+05:30",
        discrepancies=discrepancies,
        data_quality_flags=flags,
        counts=profile.model_dump(),
    )
    return reported, ground_truth


def test_write_run_creates_exactly_the_expected_files(tmp_path):
    reported, _ = _reported_world_and_ground_truth()
    out_dir = tmp_path / "runs" / "test-run"
    write_run(reported, _RATE_CARD, manifest={"run_id": "test-run"}, out_dir=out_dir, merchant_id="MERCH-TEST")

    written = {p.name for p in out_dir.iterdir()}
    assert written == {"ledger.json", "settlement_report.json", "bank_statement.json", "rate_card.md", "manifest.json"}


def test_write_run_output_round_trips_through_core_models(tmp_path):
    reported, _ = _reported_world_and_ground_truth()
    out_dir = tmp_path / "runs" / "test-run"
    write_run(reported, _RATE_CARD, manifest={"run_id": "test-run"}, out_dir=out_dir, merchant_id="MERCH-TEST")

    ledger = json.loads((out_dir / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger["payments"]) == len(reported.payments)
    for raw in ledger["payments"]:
        Payment.model_validate(raw)
    for raw in ledger["refunds"]:
        Refund.model_validate(raw)
    for raw in ledger["chargebacks"]:
        Chargeback.model_validate(raw)
    for raw in ledger["adjustments"]:
        Adjustment.model_validate(raw)

    settlement = json.loads((out_dir / "settlement_report.json").read_text(encoding="utf-8"))
    assert len(settlement["fee_lines"]) == len(reported.fee_lines)
    for raw in settlement["fee_lines"]:
        FeeLine.model_validate(raw)
    for raw in settlement["tax_lines"]:
        TaxLine.model_validate(raw)
    for raw in settlement["batches"]:
        SettlementBatch.model_validate(raw)

    bank_statement = json.loads((out_dir / "bank_statement.json").read_text(encoding="utf-8"))
    assert len(bank_statement["bank_credits"]) == len(reported.bank_credits)
    for raw in bank_statement["bank_credits"]:
        BankCredit.model_validate(raw)


def test_write_run_rate_card_markdown_contains_merchant_id(tmp_path):
    reported, _ = _reported_world_and_ground_truth()
    out_dir = tmp_path / "runs" / "test-run"
    write_run(reported, _RATE_CARD, manifest={}, out_dir=out_dir, merchant_id="MERCH-XYZ")
    md = (out_dir / "rate_card.md").read_text(encoding="utf-8")
    assert "MERCH-XYZ" in md


def test_write_ground_truth_round_trips_through_ground_truth_model(tmp_path):
    _, ground_truth = _reported_world_and_ground_truth()
    path = tmp_path / "truth" / "ground_truth.json"
    write_ground_truth(ground_truth, path)

    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    reloaded = GroundTruth.model_validate(raw)
    assert reloaded == ground_truth


def test_write_ground_truth_never_lands_inside_a_runs_directory(tmp_path):
    reported, ground_truth = _reported_world_and_ground_truth()
    run_out_dir = tmp_path / "runs" / "test-run"
    truth_path = tmp_path / "truth" / "ground_truth.json"

    write_run(reported, _RATE_CARD, manifest={}, out_dir=run_out_dir, merchant_id="MERCH-TEST")
    write_ground_truth(ground_truth, truth_path)

    assert "runs" not in truth_path.parts
    assert not (run_out_dir / "ground_truth.json").exists()
