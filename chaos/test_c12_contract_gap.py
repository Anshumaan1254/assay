"""C12 -- contract missing a clause a transaction needs.

Before this project's own review fixed it, a payment whose method/card-type
had no covering FeeRule crashed the whole audit via an uncaught
NoApplicableRule (core/contract.py::fee_for -> core/verify.py::fee_tax_cells,
called with no try/except). The fix: fee_tax_cells() catches it per payment
and reports a ContractGap instead -- naming the transaction that exposed
the gap, never falling back to a default rate, and letting the rest of the
proof's payments (and the rest of the audit) proceed normally.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from chaos.incident import chaos_scenario
from core.contract import AppliesWhen, CompiledContract, FeeRule, RateCardParse
from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier, ProofTerm
from core.ledger import Ledger
from core.models import CardType, EntityType, FeeLine, FeeType, Payment, PaymentMethod, RecordRef
from core.money import Money
from core.verify import verify

MERCHANT = "MERCH-0001"
CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _contract() -> CompiledContract:
    """Covers CARD/CREDIT only -- deliberately no clause for UPI."""
    rule = FeeRule(
        rule_id="card.credit",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT),
        rate_bps=180,
        taxes=[],
        source_quote="test",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[rule]))


def _payment(id_: str, method: PaymentMethod, card_type: CardType | None, paise: int) -> Payment:
    return Payment(
        id=id_,
        merchant_id=MERCHANT,
        amount=Money(paise),
        method=method,
        network=None,
        card_type=card_type,
        is_international=False,
        mcc="5411",
        captured_at=CAPTURED_AT,
        settlement_id="STL-1",
    )


def test_c12_a_payment_with_no_covering_clause_is_reported_not_crashed():
    with chaos_scenario(
        "C12",
        title="Contract missing a clause a transaction needs",
        category="degraded_gracefully",
        failure_injected="a UPI payment settles against a contract that only covers CARD/CREDIT",
        expected_behavior=(
            "the gap is reported with the transaction that exposed it, verify() completes for the "
            "rest of the proof's payments, and no fee is invented for the uncovered one"
        ),
    ) as scenario:
        contract = _contract()
        uncovered = _payment("PAY-UPI", PaymentMethod.UPI, None, 50_000)
        covered = _payment("PAY-CARD", PaymentMethod.CARD, CardType.CREDIT, 500_000)
        fee_line = FeeLine(
            id="FEE-1",
            applies_to_id="PAY-CARD",
            applies_to_type=EntityType.PAYMENT,
            fee_type=FeeType.MDR,
            computed_amount=Money(9_000),  # correct: 500_000 * 180bps
            rule_id="card.credit",
        )
        ledger = Ledger([uncovered, covered, fee_line])

        proof = DecompositionProof(
            credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
            credit_paise=50_000 + 500_000 - 9_000,
            currency="INR",
            tier=DecompositionTier.STRUCTURAL,
            tiers_attempted=[DecompositionTier.STRUCTURAL],
            declined_reasons=[],
            outcome=DecompositionOutcome.RESOLVED,
            reason=None,
            terms=[
                ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-UPI"), signed_paise=50_000),
                ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-CARD"), signed_paise=500_000),
                ProofTerm(ref=RecordRef(type=EntityType.FEE_LINE, id="FEE-1"), signed_paise=-9_000),
            ],
            sum_paise=50_000 + 500_000 - 9_000,
            residual_paise=0,
            competing=[],
            confidence=10_000,
            candidate_count=1,
            subset_size=3,
            nodes_expanded=0,
            assignment_cost=None,
            assignment_margin=None,
            elapsed_ns=0,
            proof_hash="test-hash",
        )

        # This call itself is the assertion: before the fix, it raised
        # NoApplicableRule uncaught and never returned.
        findings, gaps = verify(proof, ledger, contract, audit_run_id="RUN-C12")

        assert len(gaps) == 1
        assert gaps[0].payment_ref == RecordRef(type=EntityType.PAYMENT, id="PAY-UPI")
        assert gaps[0].method is PaymentMethod.UPI
        assert "no clause" in gaps[0].reason
        assert findings == [], "the covered payment matches the contract exactly -- no finding invented"

        scenario.note(f"gap reason: {gaps[0].reason}")
