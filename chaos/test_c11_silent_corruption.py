"""C11 -- silent corruption: one paise flipped on one record.

datagen/inject.py::apply_silent_corruption flips exactly +-1 paise on one
random Money field, uncompensated, across Payment/Refund/Chargeback/
Adjustment/FeeLine/TaxLine/SettlementBatch/BankCredit -- the sharpest test
of invariant 3, since nothing else in the settlement report changes to
compensate for it.

What "conservation catches it" actually means depends on which field was
hit, and this test proves both halves honestly rather than overclaiming
one shape for the other:

  - A FeeLine/TaxLine corruption IS independently recomputable against the
    contract (core/verify.py::fee_tax_cells), so it surfaces as a proper
    named Finding that pinpoints the exact record.
  - Any other field (here, the Payment itself) has no independently
    recomputable "correct" value -- it only ever surfaces as a nonzero
    conservation residual on the one proof/credit it corrupted, with that
    proof (not a single further-disambiguated record) as far as it can be
    narrowed for a multi-record batch.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chaos.incident import chaos_scenario
from core.conserve import conserve
from core.contract import AppliesWhen, CompiledContract, FeeRule, RateCardParse
from core.decompose import decompose
from core.ledger import Ledger
from core.models import (
    BankCredit,
    BatchStatus,
    CardType,
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    RecordRef,
    SettlementBatch,
)
from core.money import Money
from core.verify import verify

MERCHANT = "MERCH-0001"
CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _contract() -> CompiledContract:
    rule = FeeRule(
        rule_id="card.credit",
        fee_type=FeeType.MDR,
        effective_from=CAPTURED_AT.date(),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT),
        rate_bps=180,
        taxes=[],
        source_quote="test",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[rule]))


def _ledger(payment_paise: int, fee_paise: int, credit_paise: int) -> Ledger:
    payment = Payment(
        id="PAY-1", merchant_id=MERCHANT, amount=Money(payment_paise), method=PaymentMethod.CARD,
        network=None, card_type=CardType.CREDIT, is_international=False, mcc="5411",
        captured_at=CAPTURED_AT, settlement_id="STL-1",
    )
    fee_line = FeeLine(
        id="FEE-1", applies_to_id="PAY-1", applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR, computed_amount=Money(fee_paise), rule_id="card.credit",
    )
    batch = SettlementBatch(
        id="STL-1", merchant_id=MERCHANT, cycle_start=CAPTURED_AT, cycle_end=CAPTURED_AT,
        expected_credit=Money(credit_paise), utr="UTR-1", status=BatchStatus.SETTLED,
    )
    credit = BankCredit(id="BC-1", utr="UTR-1", amount=Money(credit_paise), value_date=CAPTURED_AT.date(), narration="NEFT")
    return Ledger([payment, fee_line, batch, credit])


def test_c11_fee_line_corruption_produces_a_named_finding_pinpointing_the_record():
    with chaos_scenario(
        "C11",
        title="Silent corruption -- one paise flipped",
        category="money_corruption",
        failure_injected="FeeLine.computed_amount silently flipped by +1 paise, nothing else adjusted",
        expected_behavior="core/verify.py's contract recomputation names the exact record and amount",
    ) as scenario:
        # Correct MDR = 500_000 * 180bps // 10_000 = 9_000. Reported: 9_001.
        ledger = _ledger(payment_paise=500_000, fee_paise=9_001, credit_paise=500_000 - 9_001)
        contract = _contract()
        credit_ref = RecordRef(type=EntityType.BANK_CREDIT, id="BC-1")
        proof = decompose(ledger.get(credit_ref), ledger, merchant_id=MERCHANT)
        assert proof.residual_paise == 0, "the settlement's own arithmetic is self-consistent"

        findings, gaps = verify(proof, ledger, contract, audit_run_id="RUN-C11-FEE")

        assert gaps == []
        assert len(findings) == 1
        assert findings[0].amount_impact == Money(1)
        assert findings[0].evidence_ids and RecordRef(type=EntityType.FEE_LINE, id="FEE-1") in findings[0].evidence_ids
        scenario.note(f"named finding: {findings[0].discrepancy_class.value}, {findings[0].amount_impact.paise} paise on FEE-1")
        scenario.money_impact_paise = 1


def test_c11_a_non_recomputable_field_corruption_surfaces_only_as_a_residual():
    with chaos_scenario(
        "C11B",
        title="Silent corruption -- non-recomputable field",
        category="money_corruption",
        failure_injected="Payment.amount silently flipped by +1 paise; SettlementBatch/BankCredit left unchanged",
        expected_behavior=(
            "no Finding is fabricated (there is no correct payment amount to recompute); the 1 paise "
            "surfaces as a nonzero conservation residual on exactly the affected proof/credit"
        ),
    ) as scenario:
        # Payment corrupted from 500_000 to 500_001; the batch/credit still
        # expect 500_000 - 9_000, exactly datagen's "deliberately never
        # recomputes any batch/bank-credit total afterward."
        ledger = _ledger(payment_paise=500_001, fee_paise=9_000, credit_paise=500_000 - 9_000)
        contract = _contract()
        credit_ref = RecordRef(type=EntityType.BANK_CREDIT, id="BC-1")
        proof = decompose(ledger.get(credit_ref), ledger, merchant_id=MERCHANT)

        findings, gaps = verify(proof, ledger, contract, audit_run_id="RUN-C11-PAY")
        report = conserve(proof, ledger)

        assert findings == [], "no Finding is invented for a field with no recomputable correct value"
        assert gaps == []
        assert report.unexplained_paise == -1, "the 1 paise is visible, attributed to exactly this credit"
        assert report.credit_ref == credit_ref, "the affected proof is identifiable even though not a single record"

        scenario.note(f"credit {credit_ref.id}: unexplained_paise={report.unexplained_paise}")
        scenario.money_impact_paise = abs(report.unexplained_paise)
