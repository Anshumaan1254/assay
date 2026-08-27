"""C06 -- truncated UTR.

A BankCredit whose UTR was corrupted/truncated in transit (datagen's D09
shape) has no exact structural match to any SettlementBatch. Tier 1's exact-
equality-only UTR lookup must NOT be repaired with a prefix match -- that
would hide a real data-quality problem behind a confident answer -- so it
falls through, declared, to tier 2/3, which resolve by amount instead of
by key. This proves the fall-through happens and is recorded, never
guessed at silently.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from chaos.incident import chaos_scenario
from core.decompose import DecompositionOutcome, DecompositionReason, DecompositionTier, decompose
from core.ledger import Ledger
from core.models import (
    BankCredit,
    BatchStatus,
    EntityType,
    Payment,
    PaymentMethod,
    RecordRef,
    SettlementBatch,
)
from core.money import Money

MERCHANT = "MERCH-0001"
DAY = date(2026, 7, 1)
VALUE_DATE = DAY + timedelta(days=2)
CAPTURED_AT = datetime(DAY.year, DAY.month, DAY.day, 12, 0, tzinfo=UTC)

TRUE_UTR = "HDFC000123420260701000000"
TRUNCATED_UTR = TRUE_UTR[:12]  # datagen's D09 shape: cut, not garbled


def test_c06_a_truncated_utr_falls_through_and_flags_rather_than_guesses():
    with chaos_scenario(
        "C06",
        title="Truncated UTR",
        category="data_quality",
        failure_injected=f"BankCredit.utr corrupted from {TRUE_UTR!r} to {TRUNCATED_UTR!r}",
        expected_behavior=(
            "tier 1's exact-match UTR lookup declines (never a prefix-match repair); the credit "
            "falls through to tier 2/3 and still resolves correctly by amount"
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
        batch = SettlementBatch(
            id="STL-1",
            merchant_id=MERCHANT,
            cycle_start=CAPTURED_AT,
            cycle_end=CAPTURED_AT,
            expected_credit=Money(500_000),
            utr=TRUE_UTR,
            status=BatchStatus.SETTLED,
        )
        credit = BankCredit(
            id="BC-1",
            utr=TRUNCATED_UTR,
            amount=Money(500_000),
            value_date=VALUE_DATE,
            narration="NEFT settlement (UTR corrupted in transit)",
        )
        ledger = Ledger([payment, batch, credit])

        proof = decompose(credit, ledger, merchant_id=MERCHANT)

        assert DecompositionTier.STRUCTURAL in proof.tiers_attempted
        assert DecompositionReason.STRUCTURAL_KEY_NOT_FOUND in proof.declined_reasons
        assert proof.tier is not DecompositionTier.STRUCTURAL, "must not fabricate a structural match"
        assert proof.outcome is DecompositionOutcome.RESOLVED, "tier 2/3 must still find the real answer"
        assert proof.residual_paise == 0
        assert {t.ref for t in proof.terms} == {RecordRef(type=EntityType.PAYMENT, id="PAY-1")}

        scenario.note(f"resolved at tier={proof.tier.value} after declining {proof.declined_reasons}")
