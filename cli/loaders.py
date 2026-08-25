"""Loads a run directory into the objects core/'s engine functions operate
on: a merchant's transaction ledger, a settlement report, a bank statement,
and a contracted rate card -- the four inputs CLAUDE.md's opening paragraph
names. No `datagen` import: this reads already-written JSON/markdown files,
the same shape a real ingest path would produce (invariant 5's quarantine).
`tests/test_decompose.py`'s own `_load_committed_run()` helper -- kept
test-side there deliberately, before anything needed a real loader -- uses
this exact same parsing shape; this module is where that job belongs now
that `cli/explain` actually needs one.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.contract import CompiledContract
from core.lanes import CalibrationArtifact, CalibrationMissing, load_calibration
from core.ledger import Ledger
from core.models import (
    Adjustment,
    BankCredit,
    Chargeback,
    EntityType,
    FeeLine,
    Payment,
    Refund,
    SettlementBatch,
    TaxLine,
)
from llm.contract_parser import compile_rate_card
from llm.provider import LLMProvider
from llm.providers.cached import CachedProvider
from llm.providers.gemini import GeminiProvider

DEFAULT_RUN_DIR = Path("runs/realistic-seed42")


def default_provider() -> LLMProvider:
    return CachedProvider(GeminiProvider())


def load_bank_credits(run_dir: Path) -> list[BankCredit]:
    statement = json.loads((run_dir / "bank_statement.json").read_text(encoding="utf-8"))
    return [BankCredit.model_validate(r) for r in statement["bank_credits"]]


def load_ledger(run_dir: Path) -> Ledger:
    ledger_json = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    report_json = json.loads((run_dir / "settlement_report.json").read_text(encoding="utf-8"))

    records: list = list(load_bank_credits(run_dir))
    for key, model in (
        ("payments", Payment),
        ("refunds", Refund),
        ("chargebacks", Chargeback),
        ("adjustments", Adjustment),
    ):
        records.extend(model.model_validate(r) for r in ledger_json[key])
    for key, model in (("fee_lines", FeeLine), ("tax_lines", TaxLine), ("batches", SettlementBatch)):
        records.extend(model.model_validate(r) for r in report_json[key])
    return Ledger(records)


def load_contract(run_dir: Path, provider: LLMProvider) -> CompiledContract:
    document = (run_dir / "rate_card.md").read_text(encoding="utf-8")
    return compile_rate_card(document, provider)


def load_calibration_artifact() -> CalibrationArtifact | None:
    """None when no calibration artifact is available -- explain() still
    works, it just shows raw (uncalibrated) proof/finding confidence."""
    try:
        return load_calibration()
    except CalibrationMissing:
        return None


def infer_merchant_id(ledger: Ledger) -> str:
    """BankCredit carries no merchant_id; every other contributing record
    does. Picks the lexicographically first Payment (or, failing that,
    SettlementBatch) so the choice is deterministic across runs."""
    for ref in ledger.by_type(EntityType.PAYMENT):
        return ledger.get(ref).merchant_id
    for ref in ledger.by_type(EntityType.SETTLEMENT_BATCH):
        return ledger.get(ref).merchant_id
    raise ValueError("cannot infer merchant_id: no Payment or SettlementBatch records in this run")
