"""Ground-truth matching: turn one synthetic run's pipeline output plus its
planted discrepancies into labeled calibration points, one per raw
confidence source. This and eval/calibrate_lanes.py are the only modules
allowed to read datagen/ ground truth (invariant 5, enforced dynamically in
tests/test_architecture.py) -- everything below is comparison logic over
RecordRefs, not generation.

Three raw sources, three labeling rules, each grounded in what that source
is actually claiming:

  - a decomposition proof claims "these are the right records for this
    credit" -- label=True unless a ground-truth MISSING_TRANSACTION or
    DUPLICATE_SETTLEMENT entry (the two classes that corrupt WHICH records
    a credit resolves to, as opposed to what they're worth) touches the
    proof's own claimed records. An AMBIGUOUS/UNRESOLVED proof claims
    nothing, so it labels False -- consistent with its raw_bps already
    being 0 (core/decompose.py's CONFIDENCE_BY_TIER only applies to
    RESOLVED).
  - a verify.py cell claims "reported equals recomputed, or it doesn't" --
    an exact-match cell cannot itself be a planted fee/tax discrepancy (a
    plant always creates a delta by construction), so it labels True
    unconditionally; a mismatching cell labels True only if a ground-truth
    entry of the matching class actually touches its evidence.
  - an adjudicator hypothesis claims a specific (class, evidence) pair --
    label=True only if BOTH match a ground-truth entry: citing the right
    record for the wrong reason, or the right reason with the wrong
    record, is still a wrong hypothesis.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel

from core.decompose import DecompositionOutcome, DecompositionProof
from core.exceptions import DiscrepancyClass
from core.lanes import SourceKind
from core.models import RecordRef
from core.verify import CONFIDENCE as VERIFY_CONFIDENCE
from core.verify import FeeTaxCell
from datagen.ground_truth import DiscrepancyEntry
from llm.adjudicator import AdjudicationRun

_TIER_SOURCE = {
    "structural": SourceKind.DECOMPOSE_STRUCTURAL,
    "subset_sum": SourceKind.DECOMPOSE_SUBSET_SUM,
    "assignment": SourceKind.DECOMPOSE_ASSIGNMENT,
}

_RECORD_SET_CORRUPTING_CLASSES = frozenset(
    {DiscrepancyClass.MISSING_TRANSACTION, DiscrepancyClass.DUPLICATE_SETTLEMENT}
)
_FEE_CELL_CLASSES = frozenset({DiscrepancyClass.FEE_OVERCHARGE, DiscrepancyClass.FEE_UNDERCHARGE})
_TAX_CELL_CLASSES = frozenset({DiscrepancyClass.TAX_MISCALCULATION})


class LabeledPoint(BaseModel):
    source: SourceKind
    raw_bps: int
    label: bool
    seed: int


def _records_by_class(ground_truth: Sequence[DiscrepancyEntry]) -> dict[DiscrepancyClass, set[RecordRef]]:
    by_class: dict[DiscrepancyClass, set[RecordRef]] = defaultdict(set)
    for entry in ground_truth:
        by_class[entry.discrepancy_class].update(entry.records)
    return by_class


def label_proofs(
    proofs: Sequence[DecompositionProof], ground_truth: Sequence[DiscrepancyEntry], seed: int
) -> list[LabeledPoint]:
    corrupted: set[RecordRef] = set()
    for entry in ground_truth:
        if entry.discrepancy_class in _RECORD_SET_CORRUPTING_CLASSES:
            corrupted.update(entry.records)

    points: list[LabeledPoint] = []
    for proof in proofs:
        source = _TIER_SOURCE[proof.tier.value]
        if proof.outcome is not DecompositionOutcome.RESOLVED:
            label = False
        else:
            claimed = {term.ref for term in proof.terms}
            claimed.add(proof.credit_ref)
            label = corrupted.isdisjoint(claimed)
        points.append(LabeledPoint(source=source, raw_bps=proof.confidence, label=label, seed=seed))
    return points


def label_verify_cells(
    cells: Sequence[FeeTaxCell], ground_truth: Sequence[DiscrepancyEntry], seed: int
) -> list[LabeledPoint]:
    planted = _records_by_class(ground_truth)

    points: list[LabeledPoint] = []
    for cell in cells:
        if cell.reported_paise == cell.recomputed_paise:
            label = True
        else:
            candidate_classes = _FEE_CELL_CLASSES if cell.kind == "fee" else _TAX_CELL_CLASSES
            planted_refs: set[RecordRef] = set()
            for discrepancy_class in candidate_classes:
                planted_refs |= planted[discrepancy_class]
            label = bool(planted_refs & set(cell.evidence_ids))
        points.append(
            LabeledPoint(source=SourceKind.VERIFY_DETERMINISTIC, raw_bps=VERIFY_CONFIDENCE, label=label, seed=seed)
        )
    return points


def label_adjudicated_hypotheses(
    run: AdjudicationRun, ground_truth: Sequence[DiscrepancyEntry], seed: int
) -> list[LabeledPoint]:
    planted = _records_by_class(ground_truth)

    points: list[LabeledPoint] = []
    for result in run.results:
        for hypothesis in result.accepted_hypotheses:
            planted_refs = planted.get(hypothesis.discrepancy_class, set())
            label = bool(planted_refs & set(hypothesis.cited_evidence))
            points.append(
                LabeledPoint(
                    source=SourceKind.ADJUDICATOR_HYPOTHESIS,
                    raw_bps=hypothesis.coverage_bps,
                    label=label,
                    seed=seed,
                )
            )
    return points
