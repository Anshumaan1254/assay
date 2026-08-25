"""Tests for cli/explain.py -- `assay explain <record_id>`.

Exercises `explain_record()` directly against the committed
runs/realistic-seed42 fixture, rather than through Typer's CliRunner and
cli/loaders.py's default_provider(). That keeps the suite independent of
whether a real Gemini API key is configured in this environment: the
committed `.llm_cache/` already has a cached response for this exact rate
card (confirmed via CachedProvider(NullProvider()), which never touches the
network), so tests load the contract through that instead of
GeminiProvider(). The real `assay explain` command is exercised as a live,
manual check separately -- see DECISIONS.md.

PAY-004509 (a planted D01: fee undercharged, correct tier 195bps applied as
160bps, Rs 4.95 impact) and PAY-000001 (untouched by any planted
discrepancy) were picked by inspecting truth/ground_truth.json directly, not
by importing it here -- this file never reads truth/ itself (invariant 5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.explain import explain_record
from cli.loaders import infer_merchant_id, load_bank_credits, load_contract, load_ledger
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = REPO_ROOT / "runs" / "realistic-seed42"

LEDGER = load_ledger(RUN_DIR)
CREDITS = load_bank_credits(RUN_DIR)
CONTRACT = load_contract(RUN_DIR, CachedProvider(NullProvider()))  # hits the committed cache
MERCHANT_ID = infer_merchant_id(LEDGER)


@pytest.mark.timeout(120)
def test_explain_a_clean_payment_shows_no_discrepancy():
    output = explain_record("PAY-000001", LEDGER, CREDITS, CONTRACT, MERCHANT_ID)

    assert "Record: payment PAY-000001" in output
    assert "No discrepancy found for this record." in output


@pytest.mark.timeout(120)
def test_explain_a_planted_underpriced_fee_shows_the_finding():
    # Ground truth's Rs 4.95 total (495 paise) splits across two findings
    # verify.py computes independently: the fee itself, undercharged by
    # 420 paise (the wrong-tier rate applied), plus the 75-paise knock-on
    # tax miscalculation on that smaller fee base -- 420 + 75 = 495.
    output = explain_record("PAY-004509", LEDGER, CREDITS, CONTRACT, MERCHANT_ID)

    assert "Record: payment PAY-004509" in output
    assert "fee_undercharge" in output
    assert "tax_miscalculation" in output
    assert "4.20" in output
    assert "0.75" in output


@pytest.mark.timeout(120)
def test_explain_an_unknown_record_id_says_so_plainly():
    output = explain_record("NOPE-DOES-NOT-EXIST", LEDGER, CREDITS, CONTRACT, MERCHANT_ID)

    assert output == "record 'NOPE-DOES-NOT-EXIST' was not found in this run's ledger."


@pytest.mark.timeout(120)
def test_explain_shows_the_credit_tier_and_proof_hash():
    output = explain_record("PAY-000001", LEDGER, CREDITS, CONTRACT, MERCHANT_ID)

    assert "Settled in credit:" in output
    assert "Decomposition tier:" in output
    assert "Proof hash:" in output
