"""Tests for eval/labels.py.

The only module besides eval/calibrate_lanes.py allowed to read datagen/
ground truth (tests/test_architecture.py enforces this dynamically over
every top-level package). Fixtures are hand-built, not run through a real
synthetic world -- each test isolates exactly one labeling rule.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier, ProofTerm
from core.exceptions import DiscrepancyClass
from core.lanes import SourceKind
from core.models import EntityType, FeeType, RecordRef
from core.verify import CONFIDENCE as VERIFY_CONFIDENCE
from core.verify import FeeTaxCell
from datagen.ground_truth import DiscrepancyEntry
from eval.labels import label_adjudicated_hypotheses, label_proofs, label_verify_cells
from llm.adjudicator import AdjudicatedHypothesis, AdjudicationResult, AdjudicationRun

CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
VALUE_DATE = date(2026, 7, 12)


def _proof(
    outcome=DecompositionOutcome.RESOLVED,
    tier=DecompositionTier.STRUCTURAL,
    terms: list[ProofTerm] | None = None,
    credit_id="BC-1",
    confidence=10_000,
) -> DecompositionProof:
    terms = terms or []
    total = sum(t.signed_paise for t in terms)
    return DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit_id),
        credit_paise=total, currency="INR",
        tier=tier, tiers_attempted=[tier], declined_reasons=[], outcome=outcome, reason=None,
        terms=terms, sum_paise=total, residual_paise=0, competing=[],
        confidence=confidence if outcome is DecompositionOutcome.RESOLVED else 0,
        candidate_count=1, subset_size=len(terms), nodes_expanded=0,
        assignment_cost=None, assignment_margin=None, elapsed_ns=0, proof_hash="test-hash",
    )


def _entry(discrepancy_class: DiscrepancyClass, records: list[RecordRef], code="D01") -> DiscrepancyEntry:
    return DiscrepancyEntry(code=code, discrepancy_class=discrepancy_class, records=records, amount_impact_paise=1_000, detail={})


PAY_1 = RecordRef(type=EntityType.PAYMENT, id="PAY-1")
PAY_2 = RecordRef(type=EntityType.PAYMENT, id="PAY-2")


# ---------------------------------------------------------------------------
# label_proofs
# ---------------------------------------------------------------------------


def test_a_resolved_proof_untouched_by_corrupting_ground_truth_labels_true():
    proof = _proof(terms=[ProofTerm(ref=PAY_1, signed_paise=500_000)])

    points = label_proofs([proof], ground_truth=[], seed=1)

    assert len(points) == 1
    assert points[0].label is True
    assert points[0].source is SourceKind.DECOMPOSE_STRUCTURAL
    assert points[0].raw_bps == 10_000
    assert points[0].seed == 1


def test_a_resolved_proof_touched_by_duplicate_settlement_ground_truth_labels_false():
    proof = _proof(terms=[ProofTerm(ref=PAY_1, signed_paise=500_000)])
    ground_truth = [_entry(DiscrepancyClass.DUPLICATE_SETTLEMENT, [PAY_1])]

    points = label_proofs([proof], ground_truth, seed=1)

    assert points[0].label is False


def test_a_resolved_proof_touched_by_missing_transaction_ground_truth_labels_false():
    proof = _proof(terms=[ProofTerm(ref=PAY_1, signed_paise=500_000)])
    ground_truth = [_entry(DiscrepancyClass.MISSING_TRANSACTION, [PAY_1])]

    points = label_proofs([proof], ground_truth, seed=1)

    assert points[0].label is False


def test_a_resolved_proof_is_unaffected_by_a_non_corrupting_ground_truth_class():
    # FEE_OVERCHARGE means the amount is wrong, not that decompose picked
    # the wrong record set -- verify.py's problem, not decompose's.
    proof = _proof(terms=[ProofTerm(ref=PAY_1, signed_paise=500_000)])
    ground_truth = [_entry(DiscrepancyClass.FEE_OVERCHARGE, [PAY_1])]

    points = label_proofs([proof], ground_truth, seed=1)

    assert points[0].label is True


def test_ambiguous_and_unresolved_proofs_always_label_false():
    ambiguous = _proof(outcome=DecompositionOutcome.AMBIGUOUS, tier=DecompositionTier.SUBSET_SUM)
    unresolved = _proof(outcome=DecompositionOutcome.UNRESOLVED, tier=DecompositionTier.ASSIGNMENT, credit_id="BC-2")

    points = label_proofs([ambiguous, unresolved], ground_truth=[], seed=1)

    assert [p.label for p in points] == [False, False]
    assert [p.raw_bps for p in points] == [0, 0]
    assert points[0].source is SourceKind.DECOMPOSE_SUBSET_SUM
    assert points[1].source is SourceKind.DECOMPOSE_ASSIGNMENT


# ---------------------------------------------------------------------------
# label_verify_cells
# ---------------------------------------------------------------------------


def _cell(kind: str, reported: int, recomputed: int, evidence: list[RecordRef]) -> FeeTaxCell:
    return FeeTaxCell(
        payment_ref=PAY_1, fee_type=FeeType.MDR, kind=kind,
        reported_paise=reported, recomputed_paise=recomputed, evidence_ids=evidence,
    )


def test_a_matching_cell_always_labels_true():
    cell = _cell("fee", reported=8_000, recomputed=8_000, evidence=[PAY_1])

    points = label_verify_cells([cell], ground_truth=[], seed=1)

    assert points[0].label is True
    assert points[0].source is SourceKind.VERIFY_DETERMINISTIC
    assert points[0].raw_bps == VERIFY_CONFIDENCE


def test_a_mismatching_fee_cell_with_matching_ground_truth_labels_true():
    cell = _cell("fee", reported=9_000, recomputed=8_000, evidence=[PAY_1])
    ground_truth = [_entry(DiscrepancyClass.FEE_OVERCHARGE, [PAY_1])]

    points = label_verify_cells([cell], ground_truth, seed=1)

    assert points[0].label is True


def test_a_mismatching_fee_cell_with_no_matching_ground_truth_labels_false():
    cell = _cell("fee", reported=9_000, recomputed=8_000, evidence=[PAY_1])

    points = label_verify_cells([cell], ground_truth=[], seed=1)

    assert points[0].label is False


def test_a_mismatching_tax_cell_only_matches_tax_miscalculation_ground_truth():
    cell = _cell("tax", reported=1_500, recomputed=1_440, evidence=[PAY_1])
    wrong_class_gt = [_entry(DiscrepancyClass.FEE_OVERCHARGE, [PAY_1])]  # right record, wrong class

    points = label_verify_cells([cell], wrong_class_gt, seed=1)
    assert points[0].label is False

    right_class_gt = [_entry(DiscrepancyClass.TAX_MISCALCULATION, [PAY_1])]
    points = label_verify_cells([cell], right_class_gt, seed=1)
    assert points[0].label is True


# ---------------------------------------------------------------------------
# label_adjudicated_hypotheses
# ---------------------------------------------------------------------------


def _run(discrepancy_class: DiscrepancyClass, cited: list[RecordRef], coverage_bps=10_000) -> AdjudicationRun:
    hypothesis = AdjudicatedHypothesis(
        discrepancy_class=discrepancy_class, cited_evidence=cited, rationale="test",
        coverage_bps=coverage_bps, rank=1,
    )
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR", accepted_hypotheses=[hypothesis], rejected_count=0,
    )
    return AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)


def test_a_hypothesis_matching_class_and_records_labels_true():
    run = _run(DiscrepancyClass.UNRECONCILED_RESIDUAL, [PAY_1])
    ground_truth = [_entry(DiscrepancyClass.UNRECONCILED_RESIDUAL, [PAY_1])]

    points = label_adjudicated_hypotheses(run, ground_truth, seed=1)

    assert points[0].label is True
    assert points[0].source is SourceKind.ADJUDICATOR_HYPOTHESIS
    assert points[0].raw_bps == 10_000


def test_a_hypothesis_with_the_right_class_but_wrong_records_labels_false():
    run = _run(DiscrepancyClass.UNRECONCILED_RESIDUAL, [PAY_2])
    ground_truth = [_entry(DiscrepancyClass.UNRECONCILED_RESIDUAL, [PAY_1])]

    points = label_adjudicated_hypotheses(run, ground_truth, seed=1)

    assert points[0].label is False


def test_a_fully_rejected_residual_produces_no_labeled_points():
    # A residual where every hypothesis failed reference-check/arithmetic
    # re-verification (llm/adjudicator.py's job, not this module's) leaves
    # accepted_hypotheses empty -- nothing here for a calibration curve to
    # learn from, and nothing should be fabricated to fill the gap.
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR", accepted_hypotheses=[], rejected_count=3,
    )
    run = AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)

    assert label_adjudicated_hypotheses(run, ground_truth=[], seed=1) == []


def test_a_hypothesis_with_the_right_records_but_wrong_class_labels_false():
    run = _run(DiscrepancyClass.ROUNDING_DRIFT, [PAY_1])
    ground_truth = [_entry(DiscrepancyClass.UNRECONCILED_RESIDUAL, [PAY_1])]

    points = label_adjudicated_hypotheses(run, ground_truth, seed=1)

    assert points[0].label is False
