"""Tests for core/verify.py.

Money-critical: this is the module that catches a settlement report whose
own arithmetic is internally consistent but wrong against the contract
(D01's shape) -- the case core/conserve.py's identity check structurally
cannot see. A bug here means a wrong fee slips through silently, with the
conservation identity reporting a clean audit over it.

Written test-first per the working agreement; most tests build a
DecompositionProof by hand via `_proof()` rather than running the full
decompose() search, so each one isolates exactly the diff verify.py is
supposed to catch. The one exception is the D01 test, which runs the real
decompose() engine too, because part of what it proves is that the real
engine resolves this case structurally with zero residual before verify.py
ever sees it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from random import Random

import pytest

from core.conserve import conserve, conserve_all, signed_paise, total_unexplained_paise
from core.contract import (
    AppliesWhen,
    CompiledContract,
    FeeRule,
    RateCardParse,
    TaxBase,
    TaxTreatment,
)
from core.decompose import (
    DecompositionOutcome,
    DecompositionProof,
    DecompositionTier,
    ProofTerm,
    decompose_all,
)
from core.exceptions import DiscrepancyClass
from core.lanes import (
    CalibrationArtifact,
    IsotonicBreakpoint,
    LaneThresholds,
    SourceCalibration,
    SourceKind,
    artifact_content_hash,
    assign_lane_for_verify_finding,
    load_calibration,
)
from core.ledger import Ledger
from core.models import (
    Adjustment,
    AdjustmentKind,
    BankCredit,
    BatchStatus,
    CardType,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    TaxLine,
)
from core.money import Money
from core.verify import CONFIDENCE, UnsupportedTaxBase, fee_tax_cells, verify, verify_all
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card, render_markdown
from datagen.world import build_true_world
from llm.contract_parser import compile_rate_card
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

REPO_ROOT = Path(__file__).resolve().parent.parent

MERCHANT = "MERCH-0001"
CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
VALUE_DATE = date(2026, 7, 12)


# ---------------------------------------------------------------------------
# Local builders. Each test file in this repo builds its own -- see
# tests/test_conserve.py and tests/test_decompose.py.
# ---------------------------------------------------------------------------


def _two_tier_contract() -> CompiledContract:
    """Two disjoint, gapless CARD/CREDIT MDR bands. Confirmed against
    core/contract.py:_validate_completeness -- completeness only requires
    gapless coverage within a (method, card_type) group that has a rule, not
    coverage of every PaymentMethod, so this minimal contract constructs
    cleanly with no filler rules."""
    tier1 = FeeRule(
        rule_id="card.credit.tier1",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(
            method=PaymentMethod.CARD, card_type=CardType.CREDIT, amount_min_paise=0, amount_max_paise=200_000
        ),
        rate_bps=180,
        taxes=[TaxTreatment(tax_type="GST", rate_bps=1800, base=TaxBase.FEE_AMOUNT)],
        source_quote="Credit card up to Rs.2,000: 1.80%",
    )
    tier2 = FeeRule(
        rule_id="card.credit.tier2",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT, amount_min_paise=200_000),
        rate_bps=160,
        taxes=[TaxTreatment(tax_type="GST", rate_bps=1800, base=TaxBase.FEE_AMOUNT)],
        source_quote="Credit card above Rs.2,000: 1.60%",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[tier1, tier2]))


def _payment(id_="PAY-1", paise=500_000, captured_at=CAPTURED_AT, settlement_id="STL-1") -> Payment:
    return Payment(
        id=id_,
        merchant_id=MERCHANT,
        amount=Money(paise),
        method=PaymentMethod.CARD,
        network=None,
        card_type=CardType.CREDIT,
        is_international=False,
        mcc="5411",
        captured_at=captured_at,
        settlement_id=settlement_id,
    )


def _fee_line(id_="FEE-1", applies_to_id="PAY-1", paise=8_000, rule_id="card.credit.tier2") -> FeeLine:
    return FeeLine(
        id=id_,
        applies_to_id=applies_to_id,
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(paise),
        rule_id=rule_id,
    )


def _tax_line(id_="TAX-1", applies_to_fee_id="FEE-1", base_paise=8_000, paise=1_440) -> TaxLine:
    return TaxLine(
        id=id_,
        applies_to_fee_id=applies_to_fee_id,
        tax_type="GST",
        rate_bps=1800,
        base_amount=Money(base_paise),
        amount=Money(paise),
    )


def _chargeback(id_="CB-1", paise=20_000, stage=ChargebackStage.WON, settlement_id="STL-1") -> Chargeback:
    return Chargeback(
        id=id_,
        payment_id="PAY-1",
        amount=Money(paise),
        reason_code="4855",
        stage=stage,
        raised_at=CAPTURED_AT,
        resolved_at=CAPTURED_AT,
        settlement_id=settlement_id,
    )


def _refund(id_="REF-1", payment_id="PAY-1", paise=15_000, settlement_id="STL-1") -> Refund:
    return Refund(
        id=id_,
        payment_id=payment_id,
        amount=Money(paise),
        is_partial=False,
        created_at=CAPTURED_AT,
        settlement_id=settlement_id,
    )


def _adjustment(id_="ADJ-1", kind=AdjustmentKind.MANUAL_CREDIT, paise=20_000, settlement_id="STL-2") -> Adjustment:
    return Adjustment(
        id=id_, kind=kind, amount=Money(paise), reason="chargeback reversal", created_at=CAPTURED_AT,
        settlement_id=settlement_id,
    )


def _term(record) -> ProofTerm:
    return ProofTerm(
        ref=RecordRef(type=record.RECORD_TYPE, id=record.id), signed_paise=signed_paise(record)
    )


def _proof(
    terms: list[ProofTerm],
    credit_paise: int,
    credit_id="BC-1",
    outcome=DecompositionOutcome.RESOLVED,
) -> DecompositionProof:
    """A hand-built proof carrying only what verify() actually inspects:
    outcome and terms. Every other field is boilerplate DecompositionProof
    requires, filled with the value a real RESOLVED structural proof would
    carry, so nothing here is a lie about the shape."""
    total = sum(t.signed_paise for t in terms)
    return DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit_id),
        credit_paise=credit_paise,
        currency="INR",
        tier=DecompositionTier.STRUCTURAL,
        tiers_attempted=[DecompositionTier.STRUCTURAL],
        declined_reasons=[],
        outcome=outcome,
        reason=None,
        terms=terms,
        sum_paise=total,
        residual_paise=credit_paise - total,
        competing=[],
        confidence=10_000,
        candidate_count=1,
        subset_size=len(terms),
        nodes_expanded=0,
        assignment_cost=None,
        assignment_margin=None,
        elapsed_ns=0,
        proof_hash="test-hash",
    )


# ---------------------------------------------------------------------------
# The money shot: D01. The report's own fee+tax arithmetic is internally
# consistent (GST correctly computed off the wrongly-tiered fee) and nets
# exactly against the credit -- conserve.py alone reports a clean audit.
# Only verify.py's contract recomputation catches it.
# ---------------------------------------------------------------------------


def test_a_fee_charged_at_the_wrong_mdr_tier_leaves_zero_unexplained_but_verify_names_it():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)  # tier2 territory: correct MDR = 500_000*160//10_000 = 8_000
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier1")  # wrong tier1 rate applied: 9_000
    tax_line = _tax_line(base_paise=9_000, paise=1_620)  # GST correctly computed off the WRONG base

    credit_paise = 500_000 - 9_000 - 1_620  # the settlement's own, self-consistent arithmetic
    batch = SettlementBatch(
        id="STL-1", merchant_id=MERCHANT, cycle_start=CAPTURED_AT, cycle_end=CAPTURED_AT,
        expected_credit=Money(credit_paise), utr="UTR-1", status=BatchStatus.SETTLED,
    )
    credit = BankCredit(id="BC-1", utr="UTR-1", amount=Money(credit_paise), value_date=VALUE_DATE, narration="NEFT")
    ledger = Ledger([payment, fee_line, tax_line, batch, credit])

    proofs = decompose_all([credit], ledger, merchant_id=MERCHANT)
    proof = proofs[0]
    assert proof.tier is DecompositionTier.STRUCTURAL
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert proof.residual_paise == 0

    report = conserve(proof, ledger)
    assert report.unexplained_paise == 0  # conserve.py alone: structurally cannot see this is wrong

    findings = verify(proof, ledger, contract, audit_run_id="RUN-D01-TEST")
    by_class = {f.discrepancy_class: f for f in findings}
    assert by_class[DiscrepancyClass.FEE_OVERCHARGE].amount_impact == Money(1_000)  # 9_000 - 8_000
    assert by_class[DiscrepancyClass.TAX_MISCALCULATION].amount_impact == Money(180)  # 1_620 - 1_440
    assert len(findings) == 2


def test_a_settlement_that_matches_the_contract_produces_no_findings():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=8_000, rule_id="card.credit.tier2")
    tax_line = _tax_line(base_paise=8_000, paise=1_440)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 8_000 - 1_440)

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_fee_tax_cells_includes_matched_cells_that_verify_does_not_turn_into_findings():
    """The whole reason fee_tax_cells exists: a calibration harness needs
    to know how often this source is RIGHT, not just how it's wrong, and
    verify() itself silently drops an exact-match cell -- there is nothing
    for it to report."""
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)  # correct MDR = 8_000
    fee_line = _fee_line(paise=8_000, rule_id="card.credit.tier2")
    tax_line = _tax_line(base_paise=8_000, paise=1_440)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 8_000 - 1_440)

    cells = fee_tax_cells(proof, ledger, contract)

    assert cells, "the fixture must actually produce cells to test their shape"
    assert all(cell.reported_paise == cell.recomputed_paise for cell in cells)
    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


# ---------------------------------------------------------------------------
# Fee sign correctness, isolated from tax.
# ---------------------------------------------------------------------------


def test_a_reported_fee_higher_than_the_contract_is_a_fee_overcharge_finding():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)  # correct MDR = 8_000
    fee_line = _fee_line(paise=9_000)  # reported too high
    tax_line = _tax_line(base_paise=9_000, paise=1_440)  # tax matches the CORRECT fee -- isolates fee only
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 9_000 - 1_440)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.FEE_OVERCHARGE
    assert findings[0].amount_impact == Money(1_000)


def test_a_reported_fee_lower_than_the_contract_is_a_fee_undercharge_finding():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)  # correct MDR = 8_000
    fee_line = _fee_line(paise=7_000)  # reported too low
    tax_line = _tax_line(base_paise=7_000, paise=1_440)  # tax matches the CORRECT fee -- isolates fee only
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 7_000 - 1_440)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.FEE_UNDERCHARGE
    assert findings[0].amount_impact == Money(1_000)


def test_tax_computed_off_the_wrong_base_is_a_tax_miscalculation_finding():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=8_000)  # correct fee -- isolates tax only
    tax_line = _tax_line(base_paise=8_000, paise=1_500)  # correct tax is 1_440
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 8_000 - 1_500)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.TAX_MISCALCULATION
    assert findings[0].amount_impact == Money(60)


# ---------------------------------------------------------------------------
# Chargeback reversal cross-check.
# ---------------------------------------------------------------------------


def test_a_won_chargeback_with_no_reversal_anywhere_in_the_ledger_is_a_chargeback_mismatch():
    contract = _two_tier_contract()
    cb = _chargeback(paise=20_000, stage=ChargebackStage.WON)
    ledger = Ledger([cb])
    proof = _proof([_term(cb)], credit_paise=-20_000)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH
    assert findings[0].amount_impact == Money(20_000)
    assert findings[0].evidence_ids == [RecordRef(type=EntityType.CHARGEBACK, id="CB-1")]


def test_a_won_chargebacks_reversal_satisfies_the_check_even_in_a_different_settlement_cycle():
    # Regression test: datagen books a WON chargeback's reversal on its
    # resolution day, routinely a different settlement cycle -- and
    # therefore a different credit/proof -- than the chargeback itself. The
    # reversal here is in the LEDGER but deliberately NOT in this proof's
    # terms; the check must still find it.
    contract = _two_tier_contract()
    cb = _chargeback(paise=20_000, stage=ChargebackStage.WON, settlement_id="STL-1")
    reversal = _adjustment(paise=20_000, settlement_id="STL-2")
    ledger = Ledger([cb, reversal])
    proof = _proof([_term(cb)], credit_paise=-20_000)  # reversal NOT among these terms

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_two_won_chargebacks_of_the_same_amount_need_two_separate_reversals():
    contract = _two_tier_contract()
    cb1 = _chargeback(id_="CB-1", paise=20_000, stage=ChargebackStage.WON)
    cb2 = _chargeback(id_="CB-2", paise=20_000, stage=ChargebackStage.WON)
    reversal = _adjustment(paise=20_000)  # only one reversal for two chargebacks of the same amount
    ledger = Ledger([cb1, cb2, reversal])
    proof = _proof([_term(cb1), _term(cb2)], credit_paise=-20_000)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1  # CB-1 consumes the one reversal (sorted-ref order); CB-2 is left unmatched
    assert findings[0].evidence_ids == [RecordRef(type=EntityType.CHARGEBACK, id="CB-2")]


def test_two_won_chargebacks_of_the_same_amount_in_different_proofs_still_need_two_separate_reversals():
    # Regression test for a bug the money-auditor review found: calling
    # verify() once per proof, each building its own fresh reversal pool
    # from the whole ledger, let two DIFFERENT proofs' same-amount WON
    # chargebacks each see the one real reversal as available and both pass
    # -- even though only one of them actually had it. verify_all() must
    # share one pool, consumed once, across the whole run.
    contract = _two_tier_contract()
    cb1 = _chargeback(id_="CB-1", paise=20_000, stage=ChargebackStage.WON, settlement_id="STL-1")
    cb2 = _chargeback(id_="CB-2", paise=20_000, stage=ChargebackStage.WON, settlement_id="STL-2")
    reversal = _adjustment(paise=20_000, settlement_id="STL-3")  # only one reversal for two chargebacks
    ledger = Ledger([cb1, cb2, reversal])
    proof1 = _proof([_term(cb1)], credit_paise=-20_000, credit_id="BC-1")
    proof2 = _proof([_term(cb2)], credit_paise=-20_000, credit_id="BC-2")

    findings = verify_all([proof1, proof2], ledger, contract, audit_run_id="RUN-1")
    assert len(findings) == 1  # exactly one of the two chargebacks is genuinely unreversed
    assert findings[0].evidence_ids == [RecordRef(type=EntityType.CHARGEBACK, id="CB-2")]


def test_a_lost_chargeback_needs_no_reversal():
    contract = _two_tier_contract()
    cb = _chargeback(paise=20_000, stage=ChargebackStage.LOST)
    ledger = Ledger([cb])
    proof = _proof([_term(cb)], credit_paise=-20_000)

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


# ---------------------------------------------------------------------------
# Refund duplicate-deduction check.
#
# There is no independently recomputable "correct" refund amount to diff a
# reported one against -- a Refund is a single record, not a claimed-vs-true
# pair the way FeeLine/contract is (this module's own docstring says so).
# What IS checkable, in the same spirit as the chargeback-reversal
# cross-check just above (match by amount, because the defect leaves no
# other trace): datagen/inject.py's D04 deducts one refund from a
# settlement an EXTRA time with no ledger record to show for it -- "the
# ledger's Refund list is never duplicated", per the injector's own
# comment -- so the credit comes up short by exactly that refund's own
# amount, and residual_paise is exactly -(refund.amount.paise). A proof's
# own REFUND terms are the only thing that can ever match this shape.
# ---------------------------------------------------------------------------


def test_a_residual_matching_one_of_the_proofs_own_refund_terms_is_a_refund_mismatch():
    contract = _two_tier_contract()
    refund = _refund(paise=15_000)
    ledger = Ledger([refund])
    proof = _proof([_term(refund)], credit_paise=-15_000 - 15_000)  # short by refund's own amount again

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")

    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.REFUND_AMOUNT_MISMATCH
    assert findings[0].amount_impact == Money(15_000)
    assert findings[0].evidence_ids == [RecordRef(type=EntityType.REFUND, id="REF-1")]


def test_a_zero_residual_produces_no_refund_finding():
    contract = _two_tier_contract()
    refund = _refund(paise=15_000)
    ledger = Ledger([refund])
    proof = _proof([_term(refund)], credit_paise=-15_000)  # exactly one deduction -- no residual

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_a_residual_in_the_credits_favour_produces_no_refund_finding():
    # The opposite direction from D04 (an UNDER-deduction, not an extra
    # one). datagen plants no such class, and there is no basis here to
    # guess what it would mean, so this stays an honest unexplained
    # residual rather than a claim this check has no evidence for.
    contract = _two_tier_contract()
    refund = _refund(paise=15_000)
    ledger = Ledger([refund])
    proof = _proof([_term(refund)], credit_paise=-15_000 + 15_000)

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_a_residual_matching_no_refund_terms_amount_produces_no_finding():
    contract = _two_tier_contract()
    refund = _refund(paise=15_000)
    ledger = Ledger([refund])
    proof = _proof([_term(refund)], credit_paise=-15_000 - 777)  # residual doesn't match any refund

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_two_refunds_of_the_same_amount_produce_no_finding_rather_than_a_guess():
    # Ambiguity beats guessing, the same discipline decompose.py already
    # applies: if two refunds could equally explain the residual, neither
    # is reported rather than picking one.
    contract = _two_tier_contract()
    r1 = _refund(id_="REF-1", paise=15_000)
    r2 = _refund(id_="REF-2", paise=15_000)
    ledger = Ledger([r1, r2])
    proof = _proof([_term(r1), _term(r2)], credit_paise=-15_000 - 15_000 - 15_000)

    assert verify(proof, ledger, contract, audit_run_id="RUN-1") == []


def test_a_refund_finding_coexists_with_a_fee_mismatch_on_the_same_proof():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier2")  # wrong tier: 8_000 is correct
    tax_line = _tax_line(base_paise=8_000, paise=1_440)  # tax on the CORRECT fee -- isolates the fee delta
    refund = _refund(paise=15_000)
    ledger = Ledger([payment, fee_line, tax_line, refund])
    proof = _proof(
        [_term(payment), _term(fee_line), _term(tax_line), _term(refund)],
        credit_paise=500_000 - 9_000 - 1_440 - 15_000 - 15_000,
    )

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")

    classes = {f.discrepancy_class for f in findings}
    assert classes == {DiscrepancyClass.FEE_OVERCHARGE, DiscrepancyClass.REFUND_AMOUNT_MISMATCH}


def test_refund_findings_are_also_calibrated_when_supplied():
    contract = _two_tier_contract()
    refund = _refund(paise=15_000)
    ledger = Ledger([refund])
    proof = _proof([_term(refund)], credit_paise=-15_000 - 15_000)
    artifact = _lane_artifact()
    expected = assign_lane_for_verify_finding(CONFIDENCE, artifact)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1", calibration=artifact)

    assert len(findings) == 1
    assert findings[0].confidence == expected.calibrated_confidence_bps
    assert findings[0].lane == expected.lane


# ---------------------------------------------------------------------------
# Proofs with nothing to recompute against.
# ---------------------------------------------------------------------------


def test_ambiguous_proofs_are_skipped_without_error():
    contract = _two_tier_contract()
    proof = _proof([], credit_paise=100_000, outcome=DecompositionOutcome.AMBIGUOUS)
    assert verify(proof, Ledger([]), contract, audit_run_id="RUN-1") == []


def test_unresolved_proofs_are_skipped_without_error():
    contract = _two_tier_contract()
    proof = _proof([], credit_paise=100_000, outcome=DecompositionOutcome.UNRESOLVED)
    assert verify(proof, Ledger([]), contract, audit_run_id="RUN-1") == []


# ---------------------------------------------------------------------------
# Finding shape: no calibration invented anywhere in this module.
# ---------------------------------------------------------------------------


def test_every_finding_verify_emits_is_major_severity_propose_lane_and_full_confidence():
    """calibration=None -- verify()'s default, backward-compatible path.
    No calibration artifact loaded means no calibrated judgment is possible,
    so this stays the old hardcoded MAJOR/PROPOSE/10_000 behaviour on
    purpose. See the calibration-supplied tests below for the calibrated
    path Phase C actually wires in."""
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier1")
    tax_line = _tax_line(base_paise=9_000, paise=1_620)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 9_000 - 1_620)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1")
    assert findings, "fixture must actually produce findings to test their shape"
    for finding in findings:
        assert finding.severity.value == "major"
        assert finding.lane.value == "propose"
        assert finding.confidence == 10_000


# ---------------------------------------------------------------------------
# Phase C: calibrated confidence/lane, wired via an optional calibration
# artifact. Mirrors tests/test_lanes.py's own _artifact() builder.
# ---------------------------------------------------------------------------

_VERIFY_LANE_BREAKPOINTS = [
    IsotonicBreakpoint(raw_bps=0, calibrated_bps=0),
    IsotonicBreakpoint(raw_bps=10_000, calibrated_bps=7_000),
]


def _lane_artifact() -> CalibrationArtifact:
    calibrations = [
        SourceCalibration(
            source=source, breakpoints=_VERIFY_LANE_BREAKPOINTS, n_calibration_points=1_000, n_positive=900
        )
        for source in SourceKind
    ]
    artifact = CalibrationArtifact(
        schema_version=1,
        fitted_at="2026-08-25T00:00:00+05:30",
        seeds_used=[1, 2, 3],
        profile="realistic",
        calibrations=calibrations,
        thresholds=LaneThresholds(
            auto_min_calibrated_bps=9_000,
            escalate_max_calibrated_bps=3_000,
            auto_target_error_bps=50,
            auto_confidence_level_bps=9_500,
        ),
        artifact_sha256="",
    )
    return artifact.model_copy(update={"artifact_sha256": artifact_content_hash(artifact)})


def test_when_calibration_is_supplied_findings_use_assign_lane_for_verify_finding():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier1")
    tax_line = _tax_line(base_paise=9_000, paise=1_620)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 9_000 - 1_620)
    artifact = _lane_artifact()
    expected = assign_lane_for_verify_finding(CONFIDENCE, artifact)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1", calibration=artifact)

    assert findings, "fixture must actually produce findings to test their shape"
    for finding in findings:
        assert finding.confidence == expected.calibrated_confidence_bps
        assert finding.lane == expected.lane
        assert finding.confidence != 10_000, "calibration must actually change the placeholder value"


def test_chargeback_findings_are_also_calibrated_when_supplied():
    payment = _payment(paise=500_000)
    chargeback = _chargeback(paise=20_000, stage=ChargebackStage.WON)
    ledger = Ledger([payment, chargeback])
    proof = _proof(
        [_term(payment), _term(chargeback)], credit_paise=500_000 - 20_000
    )
    artifact = _lane_artifact()
    expected = assign_lane_for_verify_finding(CONFIDENCE, artifact)

    findings = verify(proof, ledger, _two_tier_contract(), audit_run_id="RUN-1", calibration=artifact)

    assert findings, "an unreversed WON chargeback must produce a finding"
    assert findings[0].confidence == expected.calibrated_confidence_bps
    assert findings[0].lane == expected.lane


def test_the_real_committed_calibration_artifact_routes_verify_findings_to_propose():
    """A regression guard against the actual shipped artifact, not just a
    synthetic one -- verify_deterministic's raw 10_000 calibrates to 9981
    bps per calibration/lane_calibration.v1.json, still short of the
    committed (currently null/unreachable) AUTO threshold."""
    artifact = load_calibration(REPO_ROOT / "calibration" / "lane_calibration.v1.json")
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier1")
    tax_line = _tax_line(base_paise=9_000, paise=1_620)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 9_000 - 1_620)

    findings = verify(proof, ledger, contract, audit_run_id="RUN-1", calibration=artifact)

    assert findings
    for finding in findings:
        assert finding.confidence == 9_981
        assert finding.lane.value == "propose"


def test_verify_all_threads_calibration_to_every_finding():
    contract = _two_tier_contract()
    payment = _payment(paise=500_000)
    fee_line = _fee_line(paise=9_000, rule_id="card.credit.tier1")
    tax_line = _tax_line(base_paise=9_000, paise=1_620)
    ledger = Ledger([payment, fee_line, tax_line])
    proof = _proof([_term(payment), _term(fee_line), _term(tax_line)], credit_paise=500_000 - 9_000 - 1_620)
    artifact = _lane_artifact()
    expected = assign_lane_for_verify_finding(CONFIDENCE, artifact)

    findings = verify_all([proof], ledger, contract, audit_run_id="RUN-1", calibration=artifact)

    assert findings
    for finding in findings:
        assert finding.confidence == expected.calibrated_confidence_bps
        assert finding.lane == expected.lane


def test_finding_ids_are_unique_and_stable_across_a_run():
    contract = _two_tier_contract()
    payment1 = _payment(id_="PAY-1", paise=500_000)
    fee1 = _fee_line(id_="FEE-1", applies_to_id="PAY-1", paise=9_000, rule_id="card.credit.tier1")
    tax1 = _tax_line(id_="TAX-1", applies_to_fee_id="FEE-1", base_paise=9_000, paise=1_620)
    payment2 = _payment(id_="PAY-2", paise=500_000)
    fee2 = _fee_line(id_="FEE-2", applies_to_id="PAY-2", paise=9_000, rule_id="card.credit.tier1")
    tax2 = _tax_line(id_="TAX-2", applies_to_fee_id="FEE-2", base_paise=9_000, paise=1_620)
    ledger = Ledger([payment1, fee1, tax1, payment2, fee2, tax2])

    proof1 = _proof(
        [_term(payment1), _term(fee1), _term(tax1)], credit_paise=500_000 - 9_000 - 1_620, credit_id="BC-1"
    )
    proof2 = _proof(
        [_term(payment2), _term(fee2), _term(tax2)], credit_paise=500_000 - 9_000 - 1_620, credit_id="BC-2"
    )

    first = verify_all([proof1, proof2], ledger, contract, audit_run_id="RUN-X")
    ids = [f.id for f in first]
    assert len(ids) == len(set(ids))

    second = verify_all([proof1, proof2], ledger, contract, audit_run_id="RUN-X")
    assert [f.id for f in second] == ids


def test_a_tax_levied_on_transaction_gross_raises_rather_than_mismatching():
    rule = FeeRule(
        rule_id="card.credit.grosstax",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT),
        rate_bps=100,
        taxes=[TaxTreatment(tax_type="TDS", rate_bps=100, base=TaxBase.TRANSACTION_GROSS)],
        source_quote="TDS at 1% of gross",
    )
    contract = CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[rule]))
    payment = _payment(paise=100_000)  # correct MDR = 100_000*100//10_000 = 1_000
    fee_line = _fee_line(paise=1_000, rule_id="card.credit.grosstax")  # matches -- isolates the tax path
    ledger = Ledger([payment, fee_line])
    proof = _proof([_term(payment)], credit_paise=99_000)

    with pytest.raises(UnsupportedTaxBase):
        verify(proof, ledger, contract, audit_run_id="RUN-1")


# ---------------------------------------------------------------------------
# Integration: the engine proved sound, and the committed realistic run.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_the_clean_profile_reconciles_to_zero_unexplained_and_zero_findings():
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    rng = Random(42)
    true_world = build_true_world(config, rate_card, rng)

    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "the clean profile must plant nothing -- sanity check on the fixture itself"

    records = [
        *reported_world.payments, *reported_world.refunds, *reported_world.chargebacks,
        *reported_world.adjustments, *reported_world.fee_lines, *reported_world.tax_lines,
        *reported_world.batches, *reported_world.bank_credits,
    ]
    ledger = Ledger(records)

    document = render_markdown(default_rate_card(config.month))
    contract = compile_rate_card(document, CachedProvider(NullProvider()))  # hits the committed cache

    proofs = decompose_all(reported_world.bank_credits, ledger, merchant_id=config.merchant_id)
    reports = conserve_all(proofs, ledger)
    for r in reports:
        assert r.unexplained_paise == 0, r.credit_ref.id
    assert total_unexplained_paise(reports) == 0

    findings = verify_all(proofs, ledger, contract, audit_run_id="RUN-CLEAN-TEST")
    assert findings == []


@pytest.mark.timeout(180)
def test_the_committed_realistic_run_still_has_real_unexplained_and_verify_findings():
    run = REPO_ROOT / "runs" / "realistic-seed42"
    ledger_json = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    report_json = json.loads((run / "settlement_report.json").read_text(encoding="utf-8"))
    statement_json = json.loads((run / "bank_statement.json").read_text(encoding="utf-8"))

    credits = [BankCredit.model_validate(r) for r in statement_json["bank_credits"]]
    records: list = list(credits)
    for key, model in (
        ("payments", Payment), ("refunds", Refund), ("chargebacks", Chargeback), ("adjustments", Adjustment),
    ):
        records.extend(model.model_validate(r) for r in ledger_json[key])
    for key, model in (("fee_lines", FeeLine), ("tax_lines", TaxLine), ("batches", SettlementBatch)):
        records.extend(model.model_validate(r) for r in report_json[key])
    ledger = Ledger(records)

    document = (run / "rate_card.md").read_text(encoding="utf-8")
    contract = compile_rate_card(document, CachedProvider(NullProvider()))  # hits the committed cache

    proofs = decompose_all(credits, ledger, merchant_id=MERCHANT)
    reports = conserve_all(proofs, ledger)
    findings = verify_all(proofs, ledger, contract, audit_run_id="RUN-REALISTIC-CHECK")

    assert total_unexplained_paise(reports) != 0
    assert findings != []
