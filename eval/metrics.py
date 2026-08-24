"""Calibration diagnostics over eval.labels.LabeledPoint: reliability
diagram data, expected calibration error, Brier score, and recall against
ground truth. Floats used freely -- this is eval/, not core/, and none of
this is applied at runtime; it is reported, the way EVIDENCE.md reports a
number rather than acting on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.models import RecordRef
from datagen.ground_truth import DiscrepancyEntry
from eval.labels import LabeledPoint


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo_bps: int
    bin_hi_bps: int
    count: int
    mean_confidence: float  # 0.0-1.0
    empirical_accuracy: float  # 0.0-1.0, fraction of points in this bin with label=True


def reliability_diagram_data(points: Sequence[LabeledPoint], *, n_bins: int = 10) -> list[ReliabilityBin]:
    """Bin points by raw_bps into `n_bins` equal-width buckets over
    [0, 10_000], reporting each bucket's mean predicted confidence against
    its empirical accuracy -- the standard reliability-diagram shape."""
    width = 10_000 / n_bins
    buckets: list[list[LabeledPoint]] = [[] for _ in range(n_bins)]
    for point in points:
        index = min(int(point.raw_bps / width), n_bins - 1)
        buckets[index].append(point)

    bins: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        lo = round(index * width)
        hi = round((index + 1) * width)
        if not bucket:
            bins.append(ReliabilityBin(bin_lo_bps=lo, bin_hi_bps=hi, count=0, mean_confidence=0.0, empirical_accuracy=0.0))
            continue
        mean_confidence = sum(p.raw_bps for p in bucket) / len(bucket) / 10_000
        empirical_accuracy = sum(1 for p in bucket if p.label) / len(bucket)
        bins.append(
            ReliabilityBin(
                bin_lo_bps=lo, bin_hi_bps=hi, count=len(bucket),
                mean_confidence=mean_confidence, empirical_accuracy=empirical_accuracy,
            )
        )
    return bins


def expected_calibration_error(points: Sequence[LabeledPoint], *, n_bins: int = 10) -> float:
    """Population-weighted mean |confidence - accuracy| across bins -- the
    standard ECE definition."""
    bins = reliability_diagram_data(points, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if total == 0:
        return 0.0
    return sum(b.count * abs(b.mean_confidence - b.empirical_accuracy) for b in bins) / total


def brier_score(points: Sequence[LabeledPoint]) -> float:
    """Mean squared error between predicted probability (raw_bps / 10_000)
    and the binary outcome. Lower is better; 0 is perfect."""
    if not points:
        return 0.0
    return sum(((point.raw_bps / 10_000) - (1.0 if point.label else 0.0)) ** 2 for point in points) / len(points)


def recall_against_ground_truth(ground_truth: Sequence[DiscrepancyEntry], detected_refs: Sequence[RecordRef]) -> float:
    """Fraction of ground-truth entries with at least one of their own
    records among `detected_refs` -- records the pipeline surfaced in SOME
    form (a Finding's evidence, an accepted hypothesis's citation, a
    reported residual). An entry with none of its records detected was
    missed entirely: invisible to every other metric here, since there is
    no raw_bps to have calibrated in the first place."""
    if not ground_truth:
        return 1.0
    detected = set(detected_refs)
    found = sum(1 for entry in ground_truth if detected.intersection(entry.records))
    return found / len(ground_truth)
