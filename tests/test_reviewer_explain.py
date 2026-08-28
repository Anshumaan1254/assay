"""The structured drill-down must agree with `assay explain`.

reviewer/explain.py renders the same causal chain as cli/explain.py, for a
browser instead of a terminal. Two renderings of one thing is two things
that can disagree, and the disagreement that would matter is a numeric
one -- so these tests check the structured view against the CLI's own text
for the same record, rather than only checking that it parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.explain import explain_record
from cli.loaders import load_bank_credits, load_calibration_artifact, load_contract, load_ledger
from core.models import EntityType
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from reviewer.derive import rupees_display
from reviewer.explain import explain_structured

RUN_DIR = Path(__file__).resolve().parent.parent / "runs" / "realistic-seed42"
MERCHANT = "MERCH-0001"


@pytest.fixture(scope="module")
def context():
    ledger = load_ledger(RUN_DIR)
    return (
        ledger,
        load_bank_credits(RUN_DIR),
        load_contract(RUN_DIR, CachedProvider(NullProvider())),
        MERCHANT,
        load_calibration_artifact(),
    )


@pytest.fixture(scope="module")
def flagged_payment(context) -> str:
    """A record the engine actually has something to say about -- an
    all-clean record would let a broken renderer pass.

    Module-scoped because each `explain_structured` call runs a full
    `decompose_all` over the run; searching once and sharing the answer
    turns three whole-run decompositions per test into one for the file.
    """
    ledger, credits, contract, merchant, calibration = context
    for ref in ledger.by_type(EntityType.PAYMENT):
        view = explain_structured(ref.id, ledger, credits, contract, merchant, calibration=calibration)
        if view.findings and view.recompute:
            return ref.id
    pytest.skip("no payment in the committed run carries a finding")


@pytest.mark.timeout(300)
def test_an_unknown_record_is_reported_not_raised(context):
    ledger, credits, contract, merchant, calibration = context
    view = explain_structured("NOPE-1", ledger, credits, contract, merchant, calibration=calibration)
    assert view.found is False
    assert view.note and "NOPE-1" in view.note
    assert view.facts == []


@pytest.mark.timeout(300)
def test_the_structured_view_agrees_with_assay_explains_own_numbers(context, flagged_payment):
    ledger, credits, contract, merchant, calibration = context
    record_id = flagged_payment

    view = explain_structured(record_id, ledger, credits, contract, merchant, calibration=calibration)
    text = explain_record(record_id, ledger, credits, contract, merchant, calibration=calibration)

    # The credit it settled into, and the proof hash, must be the same fact.
    assert view.settlement is not None
    assert view.settlement.credit_id in text
    assert view.settlement.proof_hash in text

    # Every recomputed fee/tax figure the CLI prints must appear in the
    # structured rows, as the same rupee amount.
    for row in view.recompute:
        assert row.reported.rupees in text, f"{row.label} reported {row.reported.rupees} not in CLI output"
        assert row.recomputed.rupees in text, f"{row.label} recomputed missing from CLI output"

    # And the findings must match one-for-one on class and impact.
    assert len(view.findings) == text.count("  - ")
    for finding in view.findings:
        assert finding.impact.rupees in text


@pytest.mark.timeout(300)
def test_delta_is_reported_minus_recomputed_and_flags_disagreement(context, flagged_payment):
    ledger, credits, contract, merchant, calibration = context
    view = explain_structured(
        flagged_payment, ledger, credits, contract, merchant, calibration=calibration
    )

    for row in view.recompute:
        assert row.delta.paise == row.reported.paise - row.recomputed.paise
        assert row.agrees == (row.delta.paise == 0)
    assert any(not row.agrees for row in view.recompute), "fixture should include a real discrepancy"


@pytest.mark.timeout(300)
def test_facts_are_readable_rather_than_a_json_dump(context, flagged_payment):
    ledger, credits, contract, merchant, calibration = context
    view = explain_structured(
        flagged_payment, ledger, credits, contract, merchant, calibration=calibration
    )

    labels = {fact.label for fact in view.facts}
    assert "Amount" in labels
    assert "Method" in labels
    # Money is rendered in rupees with a symbol, not as a paise integer.
    amount = next(fact for fact in view.facts if fact.label == "Amount")
    assert amount.value.startswith("₹")
    assert "paise" not in amount.value
    # Booleans read as words.
    international = next((f for f in view.facts if f.label == "International"), None)
    assert international is None or international.value in {"Yes", "No"}


@pytest.mark.timeout(300)
def test_money_helper_groups_indian_digits():
    assert rupees_display(649083677) == "₹64,90,836.77"
    assert rupees_display(-101501) == "-₹1,015.01"
    assert rupees_display(0) == "₹0.00"
    assert rupees_display(99) == "₹0.99"
