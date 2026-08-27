"""C03 -- out-of-order chargeback reversal.

A WON chargeback's reversal (a MANUAL_CREDIT adjustment) is booked on its
own resolution day, which routinely lands in an earlier or later
settlement cycle -- a different credit/proof entirely -- than the
chargeback deduction itself. core/verify.py's reversal check is built
ledger-wide (one shared multiset threaded through every proof in a run,
core/verify.py::_reversal_amount_pool), specifically so this can never be
dropped (no proof "owns" the reversal) or double-counted (two proofs can't
both consume the same single real reversal). This test puts the reversal
chronologically and cycle-wise BEFORE the chargeback it belongs to, the
literal "arriving before" shape, and proves it still resolves correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chaos.incident import chaos_scenario
from core.contract import CompiledContract, RateCardParse
from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier, ProofTerm
from core.ledger import Ledger
from core.models import Adjustment, AdjustmentKind, Chargeback, ChargebackStage, EntityType, RecordRef
from core.money import Money
from core.verify import verify_all

RAISED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
RESOLVED_AT = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)  # resolved BEFORE it was even raised, in cycle terms


def _proof(credit_id: str, terms: list[ProofTerm], credit_paise: int) -> DecompositionProof:
    total = sum(t.signed_paise for t in terms)
    return DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit_id),
        credit_paise=credit_paise,
        currency="INR",
        tier=DecompositionTier.STRUCTURAL,
        tiers_attempted=[DecompositionTier.STRUCTURAL],
        declined_reasons=[],
        outcome=DecompositionOutcome.RESOLVED,
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


def test_c03_a_reversal_booked_in_an_earlier_cycle_still_resolves_its_chargeback():
    with chaos_scenario(
        "C03",
        title="Out-of-order chargeback reversal",
        category="degraded_gracefully",
        failure_injected="a WON chargeback's reversal is booked in an earlier settlement cycle/proof",
        expected_behavior="verify_all's ledger-wide reversal pool matches it exactly once regardless of order",
    ) as scenario:
        chargeback = Chargeback(
            id="CB-1",
            payment_id="PAY-1",
            amount=Money(20_000),
            reason_code="4855",
            stage=ChargebackStage.WON,
            raised_at=RAISED_AT,
            resolved_at=RESOLVED_AT,
            settlement_id="STL-LATE",
        )
        reversal = Adjustment(
            id="ADJ-1",
            kind=AdjustmentKind.MANUAL_CREDIT,
            amount=Money(20_000),
            reason="chargeback reversal, booked ahead of the chargeback's own cycle",
            created_at=RESOLVED_AT,
            settlement_id="STL-EARLY",
        )
        ledger = Ledger([chargeback, reversal])

        # Two proofs, two different credits -- the reversal's own proof
        # carries no term for the chargeback at all; the chargeback's own
        # proof carries no term for the reversal either. Only the run-wide
        # pool connects them.
        reversal_proof = _proof(
            "BC-EARLY", [ProofTerm(ref=RecordRef(type=EntityType.ADJUSTMENT, id="ADJ-1"), signed_paise=20_000)],
            credit_paise=20_000,
        )
        chargeback_proof = _proof(
            "BC-LATE", [ProofTerm(ref=RecordRef(type=EntityType.CHARGEBACK, id="CB-1"), signed_paise=-20_000)],
            credit_paise=-20_000,
        )

        findings, _gaps = verify_all(
            [reversal_proof, chargeback_proof], ledger, _dummy_contract(), audit_run_id="RUN-C03"
        )

        assert findings == [], "the reversal must be found and consumed -- never dropped, never flagged"
        scenario.note("reversal in an earlier cycle matched the later chargeback with zero findings")


def _dummy_contract() -> CompiledContract:
    return CompiledContract.from_parse(RateCardParse(merchant_id="MERCH-0001", rules=[]))
