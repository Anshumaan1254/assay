"""C04 -- refund landing in a later settlement cycle than its payment.

datagen's refund_lag_days spans up to 14 days, routinely crossing a
month/cycle boundary -- an ordinary, legitimate refund. It must be
attributed to the cycle it was ACTUALLY deducted in (its own settlement
batch/credit), never forced back onto its payment's cycle, and the two
proofs must each resolve cleanly with zero residual despite the gap. This
is deliberately distinct from D06 (a refund attributed to the WRONG batch,
which core/verify.py's own module docstring names as undetected by
design): here the refund really does belong in the later cycle it lands
in, and decompose must get that right, not merely "balance."
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from chaos.incident import chaos_scenario
from core.decompose import DecompositionOutcome, DecompositionTier, decompose_all
from core.ledger import Ledger
from core.models import (
    BankCredit,
    BatchStatus,
    EntityType,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
)
from core.money import Money

MERCHANT = "MERCH-0001"
CYCLE_1 = date(2026, 7, 1)
CYCLE_2 = date(2026, 7, 20)  # 19 days later -- crosses the settlement cycle boundary
CAPTURED_AT = datetime(CYCLE_1.year, CYCLE_1.month, CYCLE_1.day, 12, 0, tzinfo=UTC)
REFUND_CREATED_AT = datetime(CYCLE_2.year, CYCLE_2.month, CYCLE_2.day, 12, 0, tzinfo=UTC)


def _stamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)


def test_c04_a_late_refund_is_claimed_by_the_cycle_it_actually_landed_in():
    with chaos_scenario(
        "C04",
        title="Refund landing in a later cycle than its payment",
        category="degraded_gracefully",
        failure_injected="a payment settles in cycle 1; its refund is deducted 19 days later, in cycle 2",
        expected_behavior=(
            "each cycle's credit resolves against its own records with zero residual; the refund is "
            "claimed by cycle 2's proof, not forced back onto cycle 1's; the payment_ref cross-link "
            "is readable directly off the Refund record"
        ),
    ) as scenario:
        payment = Payment(
            id="PAY-1",
            merchant_id=MERCHANT,
            amount=Money(500_000),
            method=PaymentMethod.UPI,
            network=None,
            card_type=None,
            is_international=False,
            mcc="5411",
            captured_at=CAPTURED_AT,
            settlement_id="STL-1",
        )
        refund = Refund(
            id="REF-1",
            payment_id="PAY-1",
            amount=Money(15_000),
            is_partial=True,
            created_at=REFUND_CREATED_AT,
            settlement_id="STL-2",
        )
        batch_1 = SettlementBatch(
            id="STL-1", merchant_id=MERCHANT, cycle_start=_stamp(CYCLE_1), cycle_end=_stamp(CYCLE_1),
            expected_credit=Money(500_000), utr="UTR-CYCLE-1", status=BatchStatus.SETTLED,
        )
        batch_2 = SettlementBatch(
            id="STL-2", merchant_id=MERCHANT, cycle_start=_stamp(CYCLE_2), cycle_end=_stamp(CYCLE_2),
            expected_credit=Money(-15_000), utr="UTR-CYCLE-2", status=BatchStatus.SETTLED,
        )
        credit_1 = BankCredit(
            id="BC-1", utr="UTR-CYCLE-1", amount=Money(500_000),
            value_date=CYCLE_1 + timedelta(days=2), narration="NEFT cycle 1",
        )
        credit_2 = BankCredit(
            id="BC-2", utr="UTR-CYCLE-2", amount=Money(-15_000),
            value_date=CYCLE_2 + timedelta(days=2), narration="NEFT cycle 2 -- late refund only",
        )
        ledger = Ledger([payment, refund, batch_1, batch_2, credit_1, credit_2])

        proofs = decompose_all([credit_1, credit_2], ledger, merchant_id=MERCHANT)
        by_credit = {p.credit_ref.id: p for p in proofs}

        assert refund.payment_id == "PAY-1", "the cross-link is a plain field on the ledger record"

        proof_1, proof_2 = by_credit["BC-1"], by_credit["BC-2"]
        assert proof_1.tier is DecompositionTier.STRUCTURAL
        assert proof_2.tier is DecompositionTier.STRUCTURAL
        assert proof_1.outcome is DecompositionOutcome.RESOLVED
        assert proof_2.outcome is DecompositionOutcome.RESOLVED
        assert proof_1.residual_paise == 0
        assert proof_2.residual_paise == 0
        assert {t.ref for t in proof_1.terms} == {RecordRef(type=EntityType.PAYMENT, id="PAY-1")}
        assert {t.ref for t in proof_2.terms} == {RecordRef(type=EntityType.REFUND, id="REF-1")}

        scenario.note("refund correctly claimed by its own (later) cycle's proof, zero residual on both")
