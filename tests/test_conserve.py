"""Tests for core/conserve.py's sign convention.

Money-critical: every settlement net in the engine is a sum of these signs.
A single flipped sign moves rupees between "settled" and "unexplained" with
no other symptom.

Written before `core/conserve.py` exists — expected to fail on collection
until that module is implemented.

`datagen/world.py:417 compute_batch_net_paise` is the only implementation of
this convention in the repo today, and `core/` may not import it (invariant
5). DECISIONS.md 2026-08-23 flagged that core must "either adopt this
convention or reconcile against it". This file is that reconciliation: the
last test re-derives a batch net from raw records with an independently
written formula and asserts `signed_paise` agrees.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.conserve import (
    ADD_BACK_KINDS,
    SUBTRACT_KINDS,
    ConservationViolation,
    conserve,
    conserve_all,
    signed_paise,
    total_unexplained_paise,
    unclaimed_paise,
    unclaimed_records,
)
from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier, ProofTerm
from core.ledger import Ledger
from core.models import (
    Adjustment,
    AdjustmentKind,
    AuditRun,
    BankCredit,
    BatchStatus,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    JournalEntry,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    TaxLine,
)
from core.money import Money

UTC_NOON = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _payment(id_="PAY-1", paise=100_000, settlement_id=None):
    return Payment(
        id=id_,
        merchant_id="MERCH-0001",
        amount=Money(paise),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _refund(id_="REF-1", paise=5_000, settlement_id=None):
    return Refund(
        id=id_,
        payment_id="PAY-1",
        amount=Money(paise),
        is_partial=False,
        created_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _chargeback(id_="CB-1", paise=20_000, stage=ChargebackStage.RAISED, settlement_id=None):
    return Chargeback(
        id=id_,
        payment_id="PAY-1",
        amount=Money(paise),
        reason_code="4855",
        stage=stage,
        raised_at=UTC_NOON,
        resolved_at=None,
        settlement_id=settlement_id,
    )


def _adjustment(id_="ADJ-1", kind=AdjustmentKind.MANUAL_CREDIT, paise=7_000, settlement_id=None):
    return Adjustment(
        id=id_,
        kind=kind,
        amount=Money(paise),
        reason="test",
        created_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _fee_line(id_="FEE-1", applies_to_id="PAY-1", paise=2_360):
    return FeeLine(
        id=id_,
        applies_to_id=applies_to_id,
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(paise),
        rule_id="rule_1",
    )


def _tax_line(id_="TAX-1", applies_to_fee_id="FEE-1", paise=425):
    return TaxLine(
        id=id_,
        applies_to_fee_id=applies_to_fee_id,
        tax_type="GST",
        rate_bps=1800,
        base_amount=Money(2_360),
        amount=Money(paise),
    )


# ---------------------------------------------------------------------------
# Sign per entity type. These are the six record types that contribute to a
# settlement net; everything else must raise rather than silently score 0.
# ---------------------------------------------------------------------------


def test_payment_adds_its_gross():
    assert signed_paise(_payment(paise=100_000)) == 100_000


def test_refund_subtracts():
    assert signed_paise(_refund(paise=5_000)) == -5_000


def test_fee_line_subtracts_its_computed_amount():
    # FeeLine's money field is `computed_amount`, not `amount` -- a
    # getattr(record, "amount") implementation would raise here.
    assert signed_paise(_fee_line(paise=2_360)) == -2_360


def test_tax_line_subtracts_its_amount_not_its_base():
    # TaxLine carries both `base_amount` (2_360) and `amount` (425). Only
    # the tax itself is deducted; picking base_amount would overstate the
    # deduction by the whole fee.
    assert signed_paise(_tax_line(paise=425)) == -425


@pytest.mark.parametrize("stage", list(ChargebackStage))
def test_chargeback_subtracts_at_every_stage(stage):
    # No stage-dependent sign. A won dispute's reversal is a separate
    # MANUAL_CREDIT Adjustment (ADJ-CBR-*), per DECISIONS.md 2026-08-23:
    # core/models.py has no Reversal entity, so the identity's "+ reversals"
    # term is backed by an adjustment. Making `won` positive here would
    # double-count the reversal.
    assert signed_paise(_chargeback(paise=20_000, stage=stage)) == -20_000


# ---------------------------------------------------------------------------
# Adjustment sign is resolved by kind, not by type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [AdjustmentKind.RESERVE_RELEASE, AdjustmentKind.MANUAL_CREDIT, AdjustmentKind.FEE_WAIVER],
)
def test_add_back_adjustment_kinds_are_positive(kind):
    assert signed_paise(_adjustment(kind=kind, paise=7_000)) == 7_000


@pytest.mark.parametrize("kind", [AdjustmentKind.RESERVE_HOLD, AdjustmentKind.MANUAL_DEBIT])
def test_subtract_adjustment_kinds_are_negative(kind):
    assert signed_paise(_adjustment(kind=kind, paise=7_000)) == -7_000


def test_every_adjustment_kind_has_a_declared_sign():
    # A new AdjustmentKind added to core/models.py without a sign here would
    # otherwise fall through to whatever the default branch does. Partition,
    # not just coverage: no kind may appear in both tables.
    assert ADD_BACK_KINDS | SUBTRACT_KINDS == set(AdjustmentKind)
    assert ADD_BACK_KINDS & SUBTRACT_KINDS == set()


# ---------------------------------------------------------------------------
# Types with no defined contribution raise. A silent 0 would let a mis-typed
# record vanish from a sum with no error anywhere.
# ---------------------------------------------------------------------------


def _settlement_batch():
    return SettlementBatch(
        id="STL-20260701",
        merchant_id="MERCH-0001",
        cycle_start=UTC_NOON,
        cycle_end=UTC_NOON,
        expected_credit=Money(100_000),
        utr="HDFC0001",
        status=BatchStatus.SETTLED,
    )


def _bank_credit():
    return BankCredit(
        id="BC-1",
        utr="HDFC0001",
        amount=Money(100_000),
        value_date=UTC_NOON.date(),
        narration="NEFT",
    )


def _audit_run():
    return AuditRun(
        id="RUN-1",
        started_at=UTC_NOON,
        contract_version="rc-abc",
        input_hashes={},
        seed=42,
        report_hash="deadbeef",
    )


def _journal_entry():
    return JournalEntry(
        id="JE-1",
        debit_account="a",
        credit_account="b",
        amount=Money(1),
        narration="n",
        idempotency_key="k",
        posted_at=UTC_NOON,
    )


@pytest.mark.parametrize(
    "record",
    [_settlement_batch(), _bank_credit(), _audit_run(), _journal_entry()],
    ids=["settlement_batch", "bank_credit", "audit_run", "journal_entry"],
)
def test_non_contributing_types_raise(record):
    with pytest.raises(ValueError):
        signed_paise(record)


def test_a_bank_credit_is_never_a_term_in_its_own_decomposition():
    # The specific mistake this guards: including the credit itself in the
    # subset that explains the credit, which nets to zero and "balances".
    with pytest.raises(ValueError):
        signed_paise(_bank_credit())


# ---------------------------------------------------------------------------
# Reconciliation against datagen's convention (DECISIONS.md 2026-08-23).
#
# Deliberately NOT calling datagen.world.compute_batch_net_paise: this test's
# whole point is to check core's signs against an independently written
# formula. Importing datagen's function here would make it tautological --
# and core/ could not import it anyway (invariant 5).
# ---------------------------------------------------------------------------


def test_signed_sum_equals_the_independently_written_batch_formula():
    batch_id = "STL-20260701"
    payments = [_payment("PAY-1", 500_000, batch_id), _payment("PAY-2", 300_000, batch_id)]
    refunds = [_refund("REF-1", 25_000, batch_id)]
    chargebacks = [_chargeback("CB-1", 40_000, ChargebackStage.LOST, batch_id)]
    adjustments = [
        _adjustment("ADJ-RR-1", AdjustmentKind.RESERVE_RELEASE, 10_000, batch_id),
        _adjustment("ADJ-RH-1", AdjustmentKind.RESERVE_HOLD, 15_000, batch_id),
        _adjustment("ADJ-GW-1", AdjustmentKind.FEE_WAIVER, 2_000, batch_id),
        _adjustment("ADJ-MD-1", AdjustmentKind.MANUAL_DEBIT, 3_000, batch_id),
    ]
    fee_lines = [_fee_line("FEE-1", "PAY-1", 11_800), _fee_line("FEE-2", "PAY-2", 7_080)]
    tax_lines = [_tax_line("TAX-1", "FEE-1", 2_124), _tax_line("TAX-2", "FEE-2", 1_274)]

    # gross - refunds - fees - tax - chargebacks + add_back - subtract
    gross = sum(p.amount.paise for p in payments)
    refunds_total = sum(r.amount.paise for r in refunds)
    fees = sum(f.computed_amount.paise for f in fee_lines)
    tax = sum(t.amount.paise for t in tax_lines)
    chargebacks_total = sum(c.amount.paise for c in chargebacks)
    add_back = sum(a.amount.paise for a in adjustments if a.kind in ADD_BACK_KINDS)
    subtract = sum(a.amount.paise for a in adjustments if a.kind in SUBTRACT_KINDS)
    expected = gross - refunds_total - fees - tax - chargebacks_total + add_back - subtract

    records = payments + refunds + chargebacks + adjustments + fee_lines + tax_lines
    actual = sum(signed_paise(r) for r in records)

    assert actual == expected, f"signed sum {actual} != independently computed net {expected}"


def test_the_reconciliation_fixture_is_not_accidentally_zero():
    # Guards the test above: if every term happened to cancel, both sides
    # would be 0 and the assertion would prove nothing.
    assert signed_paise(_payment("PAY-1", 500_000, "STL-20260701")) != 0


# ---------------------------------------------------------------------------
# Part two: conserve() -- bucketing a proof's terms into the identity.
#
# unexplained_paise must equal proof.residual_paise exactly (the "orthogonal
# layers" design: conserve.py checks the settlement's own arithmetic nets
# against the credit; whether that arithmetic is *correct* per the contract
# is core/verify.py's separate question).
# ---------------------------------------------------------------------------


def _term(record) -> ProofTerm:
    return ProofTerm(
        ref=RecordRef(type=record.RECORD_TYPE, id=record.id), signed_paise=signed_paise(record)
    )


def _proof(terms: list[ProofTerm], credit_paise: int, credit_id="BC-1", outcome=DecompositionOutcome.RESOLVED):
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


def test_conserve_reconstructs_credit_paise_exactly_from_its_buckets():
    payments = [_payment("PAY-1", 500_000, "STL-1"), _payment("PAY-2", 300_000, "STL-1")]
    refunds = [_refund("REF-1", 25_000, "STL-1")]
    chargebacks = [_chargeback("CB-1", 40_000, ChargebackStage.LOST, "STL-1")]
    adjustments = [
        _adjustment("ADJ-RR-1", AdjustmentKind.RESERVE_RELEASE, 10_000, "STL-1"),
        _adjustment("ADJ-RH-1", AdjustmentKind.RESERVE_HOLD, 15_000, "STL-1"),
    ]
    fee_lines = [_fee_line("FEE-1", "PAY-1", 11_800), _fee_line("FEE-2", "PAY-2", 7_080)]
    tax_lines = [_tax_line("TAX-1", "FEE-1", 2_124), _tax_line("TAX-2", "FEE-2", 1_274)]
    records = payments + refunds + chargebacks + adjustments + fee_lines + tax_lines
    ledger = Ledger(records)

    terms = [_term(r) for r in records]
    credit_paise = sum(signed_paise(r) for r in records)  # a batch that nets exactly, residual == 0
    proof = _proof(terms, credit_paise)

    report = conserve(proof, ledger)
    assert report.settled_gross_paise == 800_000
    assert report.refunds_paise == 25_000
    assert report.fees_paise == 18_880
    assert report.tax_paise == 3_398
    assert report.chargebacks_paise == 40_000
    assert report.adjustments_paise == 15_000
    assert report.reversals_paise == 10_000
    assert report.unexplained_paise == 0
    reconstructed = (
        report.settled_gross_paise - report.refunds_paise - report.fees_paise - report.tax_paise
        - report.chargebacks_paise - report.adjustments_paise + report.reversals_paise
        + report.unexplained_paise
    )
    assert reconstructed == credit_paise


def test_conserve_unexplained_equals_the_proofs_own_residual_paise():
    payment = _payment("PAY-1", 500_000, "STL-1")
    ledger = Ledger([payment])
    # A credit that does NOT net exactly against the term -- a residual on
    # purpose, mirroring a report whose reported numbers don't add up.
    proof = _proof([_term(payment)], credit_paise=450_000)

    report = conserve(proof, ledger)
    assert report.unexplained_paise == proof.residual_paise
    assert report.unexplained_paise == -50_000


def test_conserve_on_an_ambiguous_proof_puts_the_whole_credit_in_unexplained():
    proof = _proof([], credit_paise=250_000, outcome=DecompositionOutcome.AMBIGUOUS)
    report = conserve(proof, Ledger([]))
    assert report.settled_gross_paise == 0
    assert report.refunds_paise == 0
    assert report.fees_paise == 0
    assert report.tax_paise == 0
    assert report.chargebacks_paise == 0
    assert report.adjustments_paise == 0
    assert report.reversals_paise == 0
    assert report.unexplained_paise == 250_000


def test_conserve_on_an_unresolved_proof_puts_the_whole_credit_in_unexplained():
    proof = _proof([], credit_paise=250_000, outcome=DecompositionOutcome.UNRESOLVED)
    report = conserve(proof, Ledger([]))
    assert report.unexplained_paise == 250_000


def test_a_hand_corrupted_proof_raises_conservation_violation_instead_of_reporting_a_wrong_number():
    payment = _payment("PAY-1", 500_000, "STL-1")
    ledger = Ledger([payment])
    proof = _proof([_term(payment)], credit_paise=500_000)
    corrupted = proof.model_copy(update={"residual_paise": proof.residual_paise + 1})

    with pytest.raises(ConservationViolation):
        conserve(corrupted, ledger)


def test_conserve_all_returns_one_report_per_proof_in_input_order():
    payment1 = _payment("PAY-1", 500_000, "STL-1")
    payment2 = _payment("PAY-2", 300_000, "STL-2")
    ledger = Ledger([payment1, payment2])
    proof1 = _proof([_term(payment1)], credit_paise=500_000, credit_id="BC-1")
    proof2 = _proof([_term(payment2)], credit_paise=300_000, credit_id="BC-2")

    reports = conserve_all([proof1, proof2], ledger)
    assert [r.credit_ref.id for r in reports] == ["BC-1", "BC-2"]


def test_total_unexplained_paise_sums_across_a_run():
    payment1 = _payment("PAY-1", 500_000, "STL-1")
    payment2 = _payment("PAY-2", 300_000, "STL-2")
    ledger = Ledger([payment1, payment2])
    proof1 = _proof([_term(payment1)], credit_paise=480_000, credit_id="BC-1")  # residual -20_000
    proof2 = _proof([_term(payment2)], credit_paise=310_000, credit_id="BC-2")  # residual +10_000

    reports = conserve_all([proof1, proof2], ledger)
    assert total_unexplained_paise(reports) == -10_000


# ---------------------------------------------------------------------------
# unclaimed_paise() -- the run-level check. total_unexplained_paise() only
# ever sums the residual on credits that exist; a settlement batch whose
# bank credit never arrived at all contributes no proof and no residual,
# so it is invisible to total_unexplained_paise no matter how large it is.
# Found in a skeptical staff-engineer review: a whole missing batch
# reported as a perfectly clean, zero-unexplained run.
# ---------------------------------------------------------------------------


def test_a_settlement_batch_with_no_bank_credit_at_all_reports_clean_by_total_unexplained_alone():
    # STL-A actually gets paid and resolves cleanly.
    pay_a = _payment("PAY-A", 500_000, "STL-A")
    fee_a = _fee_line("FEE-A", "PAY-A", 8_000)
    tax_a = _tax_line("TAX-A", "FEE-A", 1_440)
    credit_a = BankCredit(id="BC-A", utr="UTR-A", amount=Money(490_560), value_date=date(2026, 7, 12), narration="NEFT")

    # STL-B was formed and reported -- but the bank never actually sent the
    # money. There is deliberately no BankCredit for it anywhere.
    pay_b = _payment("PAY-B", 300_000, "STL-B")
    fee_b = _fee_line("FEE-B", "PAY-B", 5_000)
    tax_b = _tax_line("TAX-B", "FEE-B", 900)

    ledger = Ledger([pay_a, fee_a, tax_a, credit_a, pay_b, fee_b, tax_b])
    proof_a = _proof([_term(pay_a), _term(fee_a), _term(tax_a)], credit_paise=490_560, credit_id="BC-A")

    reports = conserve_all([proof_a], ledger)
    assert total_unexplained_paise(reports) == 0  # the blind spot: looks perfectly clean

    missing = unclaimed_records(ledger, [proof_a])
    assert missing == [
        RecordRef(type=EntityType.FEE_LINE, id="FEE-B"),
        RecordRef(type=EntityType.PAYMENT, id="PAY-B"),
        RecordRef(type=EntityType.TAX_LINE, id="TAX-B"),
    ]
    assert unclaimed_paise(ledger, [proof_a]) == 300_000 - 5_000 - 900  # STL-B's net, never seen anywhere


def test_unclaimed_paise_is_zero_when_every_ledger_record_was_claimed():
    payment = _payment("PAY-1", 500_000, "STL-1")
    ledger = Ledger([payment])
    proof = _proof([_term(payment)], credit_paise=500_000)

    assert unclaimed_records(ledger, [proof]) == []
    assert unclaimed_paise(ledger, [proof]) == 0
