"""Serializes a World + RateCard + run manifest to runs/<run_id>/, and a
GroundTruth to truth/ground_truth.json. Two separate functions writing two
separate directory trees -- ground truth never lands inside the engine's
input path (CLAUDE.md invariant 5).
"""

from __future__ import annotations

import json
from pathlib import Path

from datagen.ground_truth import GroundTruth
from datagen.ratecard import RateCard, render_markdown
from datagen.world import World


def _dump(models) -> list[dict]:
    return [m.model_dump(mode="json") for m in models]


def write_run(world: World, rate_card: RateCard, manifest: dict, out_dir: Path, merchant_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    ledger = {
        "payments": _dump(world.payments),
        "refunds": _dump(world.refunds),
        "chargebacks": _dump(world.chargebacks),
        "adjustments": _dump(world.adjustments),
    }
    (out_dir / "ledger.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    settlement_report = {
        "fee_lines": _dump(world.fee_lines),
        "tax_lines": _dump(world.tax_lines),
        "batches": _dump(world.batches),
    }
    (out_dir / "settlement_report.json").write_text(json.dumps(settlement_report, indent=2), encoding="utf-8")

    bank_statement = {"bank_credits": _dump(world.bank_credits)}
    (out_dir / "bank_statement.json").write_text(json.dumps(bank_statement, indent=2), encoding="utf-8")

    (out_dir / "rate_card.md").write_text(render_markdown(rate_card, merchant_id=merchant_id), encoding="utf-8")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


def write_ground_truth(ground_truth: GroundTruth, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ground_truth.model_dump_json(indent=2), encoding="utf-8")
