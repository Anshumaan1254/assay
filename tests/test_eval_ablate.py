"""Tests for eval/ablate.py -- §13, the section that argues the AI earns
its place.

The claim §13 makes is comparative, so the risk is not a wrong absolute
number but an unfair comparison: an ablation that quietly keeps a safeguard
belonging to the thing being removed, or that forgets to charge a removed
component for the money it was explaining. Both are pinned below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.contract import AppliesWhen, CompiledContract, FeeRule, RateCardParse
from core.decompose import (
    DecompositionOutcome,
    DecompositionProof,
    DecompositionReason,
    DecompositionTier,
    ProofTerm,
)
from core.exceptions import DiscrepancyClass
from core.ledger import Ledger
from core.models import (
    CardType,
    EntityType,
    FeeLine,
    FeeType,
    Finding,
    Lane,
    Network,
    Payment,
    PaymentMethod,
    RecordRef,
    Severity,
)
from core.money import Money
from eval.ablate import (
    FIXED_AUTO_THRESHOLD_BPS,
    structural_only,
    structural_only_findings,
    structural_only_unexplained_paise,
    with_fixed_threshold,
    without_adjudicator,
)


def _payment(id_: str = "PAY-1", paise: int = 500_000) -> Payment:
    return Payment(
        id=id_,
        merchant_id="MERCH-0001",
        amount=Money(paise),
        method=PaymentMethod.CARD,
        network=Network.VISA,
        card_type=CardType.CREDIT,
        is_international=False,
        mcc="5411",
        captured_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
        settlement_id="STL-1",
    )


def _proof(
    credit_id: str,
    tier: DecompositionTier,
    outcome: DecompositionOutcome,
    *,
    credit_paise: int = 500_000,
    terms: list[ProofTerm] | None = None,
    residual: int = 0,
) -> DecompositionProof:
    terms = terms or []
    return DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit_id),
        credit_paise=credit_paise,
        currency="INR",
        tier=tier,
        tiers_attempted=[tier],
        declined_reasons=[],
        outcome=outcome,
        reason=None if outcome is DecompositionOutcome.RESOLVED else DecompositionReason.NO_CANDIDATES_IN_WINDOW,
        terms=terms,
        sum_paise=sum(t.signed_paise for t in terms),
        residual_paise=residual,
        competing=[],
        confidence=10_000,
        candidate_count=1,
        subset_size=len(terms),
        nodes_expanded=0,
        assignment_cost=None,
        assignment_margin=None,
        elapsed_ns=0,
        proof_hash="0" * 64,
    )


def _finding(id_: str, confidence: int, lane: Lane = Lane.PROPOSE, paise: int = 1_000) -> Finding:
    return Finding(
        id=id_,
        audit_run_id="AUD-TEST",
        discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE,
        severity=Severity.MAJOR,
        amount_impact=Money(paise),
        evidence_ids=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
        confidence=confidence,
        lane=lane,
        explanation="test",
    )


# ---------------------------------------------------------------------------
# 1. Without the adjudicator
# ---------------------------------------------------------------------------


def test_only_adjudicated_findings_are_removed():
    findings = [_finding("FND-AUD-1-BC-1-001", 10_000), _finding("FND-ADJ-AUD-1-RES-BC-2", 5_000)]
    assert [f.id for f in without_adjudicator(findings)] == ["FND-AUD-1-BC-1-001"]


# ---------------------------------------------------------------------------
# 2. Without conformal calibration
# ---------------------------------------------------------------------------


def test_the_fixed_threshold_auto_posts_at_exactly_the_round_number():
    findings = [
        _finding("A", FIXED_AUTO_THRESHOLD_BPS - 1),
        _finding("B", FIXED_AUTO_THRESHOLD_BPS),
        _finding("C", 10_000),
    ]
    lanes = {f.id: f.lane for f in with_fixed_threshold(findings)}
    assert lanes == {"A": Lane.PROPOSE, "B": Lane.AUTO, "C": Lane.AUTO}


def test_the_fixed_threshold_ablation_does_not_keep_the_llm_auto_exemption():
    """The exemption that keeps LLM-sourced items out of AUTO is a
    consequence of having calibrated the sources separately and found them
    incomparable. A system with one fixed threshold has no basis for it,
    and lending the ablation a safeguard produced by the thing being
    ablated would make the comparison meaningless."""
    adjudicated = _finding("FND-ADJ-AUD-1-RES-BC-1", 10_000)
    assert with_fixed_threshold([adjudicated])[0].lane is Lane.AUTO


def _contract() -> CompiledContract:
    rule = FeeRule(
        rule_id="card.credit",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT),
        rate_bps=180,
        taxes=[],
        source_quote="test",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id="MERCH-0001", rules=[rule]))


def test_structural_only_findings_returns_a_plain_finding_list_not_a_tuple():
    # Regression: core.verify.verify_all() now returns (findings, gaps).
    # list() on that 2-tuple silently produces [findings_list, gaps_list]
    # instead of a list of Finding objects -- syntactically iterable, so it
    # doesn't fail here, but crashes the first caller that reads .id off an
    # element (eval/detect.py's sort does exactly that).
    payment = _payment(paise=500_000)  # correct MDR at 180bps = 9_000
    fee_line = FeeLine(
        id="FEE-1",
        applies_to_id="PAY-1",
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(9_500),  # reported 500 paise too high
        rule_id="card.credit",
    )
    ledger = Ledger([payment, fee_line])
    proof = _proof(
        "BC-1",
        DecompositionTier.STRUCTURAL,
        DecompositionOutcome.RESOLVED,
        credit_paise=500_000 - 9_500,
        terms=[
            ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-1"), signed_paise=500_000),
            ProofTerm(ref=RecordRef(type=EntityType.FEE_LINE, id="FEE-1"), signed_paise=-9_500),
        ],
    )

    findings = structural_only_findings([proof], ledger, _contract(), audit_run_id="AUD-TEST")

    assert all(isinstance(f, Finding) for f in findings)
    assert len(findings) == 1
    assert findings[0].discrepancy_class is DiscrepancyClass.FEE_OVERCHARGE
    assert findings[0].amount_impact == Money(500)


# ---------------------------------------------------------------------------
# 3. Without tiers 2 and 3
# ---------------------------------------------------------------------------


def test_structural_only_keeps_exactly_the_tier_one_resolutions():
    proofs = [
        _proof("BC-1", DecompositionTier.STRUCTURAL, DecompositionOutcome.RESOLVED),
        _proof("BC-2", DecompositionTier.SUBSET_SUM, DecompositionOutcome.RESOLVED),
        _proof("BC-3", DecompositionTier.ASSIGNMENT, DecompositionOutcome.RESOLVED),
        _proof("BC-4", DecompositionTier.STRUCTURAL, DecompositionOutcome.UNRESOLVED),
    ]
    assert [p.credit_ref.id for p in structural_only(proofs)] == ["BC-1"]


def test_a_credit_tier_one_could_not_resolve_counts_as_entirely_unexplained():
    """The trap: a credit no tier resolved is not zero unexplained money,
    it is ALL unexplained money. An ablation that summed only the residual
    on the credits tier 1 did resolve would make tier 1 look far better
    than it is."""
    payment = _payment()
    ledger = Ledger([payment])
    proofs = [
        _proof(
            "BC-1",
            DecompositionTier.STRUCTURAL,
            DecompositionOutcome.RESOLVED,
            credit_paise=500_000,
            terms=[ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-1"), signed_paise=500_000)],
            residual=0,
        ),
        _proof("BC-2", DecompositionTier.SUBSET_SUM, DecompositionOutcome.RESOLVED, credit_paise=250_000),
    ]

    assert structural_only_unexplained_paise(proofs, ledger) == 250_000


def test_structural_only_unexplained_includes_the_kept_proofs_own_residual():
    payment = _payment()
    ledger = Ledger([payment])
    proofs = [
        _proof(
            "BC-1",
            DecompositionTier.STRUCTURAL,
            DecompositionOutcome.RESOLVED,
            credit_paise=500_000,
            terms=[ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-1"), signed_paise=499_000)],
            residual=1_000,
        )
    ]

    assert structural_only_unexplained_paise(proofs, ledger) == 1_000


def test_structural_only_over_an_empty_run_is_zero_not_an_error():
    assert structural_only_unexplained_paise([], Ledger([])) == 0


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def test_the_ablation_table_reports_signed_deltas_against_the_baseline():
    from core.models import IST
    from datagen.ground_truth import GroundTruth
    from eval.ablate import AblationReport, AblationRow
    from eval.detect import evaluate

    truth = GroundTruth(
        run_id="t", seed=1, profile="t", generated_at=datetime(2026, 7, 1, tzinfo=IST), discrepancies=[]
    )
    baseline = evaluate([], truth)
    worse = evaluate([_finding("F1", 10_000, paise=5_000)], truth)  # a pure false positive

    report = AblationReport(
        baseline=baseline,
        rows=[AblationRow(name="x", label="x disabled", description="d", detection=worse)],
    )

    assert report.delta_false_positive_paise(report.rows[0]) == 5_000
    assert report.delta_value_recall(report.rows[0]) == pytest.approx(0.0)
