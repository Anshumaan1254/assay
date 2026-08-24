"""Tests for eval/metrics.py: textbook, hand-computed values. Floats are
fine here -- this is eval/, not core/.
"""

from __future__ import annotations

import pytest

from core.exceptions import DiscrepancyClass
from core.lanes import SourceKind
from core.models import EntityType, RecordRef
from datagen.ground_truth import DiscrepancyEntry
from eval.labels import LabeledPoint
from eval.metrics import (
    brier_score,
    expected_calibration_error,
    recall_against_ground_truth,
    reliability_diagram_data,
)

PAY_1 = RecordRef(type=EntityType.PAYMENT, id="PAY-1")
PAY_2 = RecordRef(type=EntityType.PAYMENT, id="PAY-2")
PAY_3 = RecordRef(type=EntityType.PAYMENT, id="PAY-3")
PAY_4 = RecordRef(type=EntityType.PAYMENT, id="PAY-4")


def _point(raw_bps: int, label: bool) -> LabeledPoint:
    return LabeledPoint(source=SourceKind.VERIFY_DETERMINISTIC, raw_bps=raw_bps, label=label, seed=1)


# Two bins, [0,5000) and [5000,10000]:
#   bin0: raw_bps 1000 (True), 2000 (False) -> mean_conf 0.15, accuracy 0.5
#   bin1: raw_bps 9000 (True), 8000 (True)  -> mean_conf 0.85, accuracy 1.0
_POINTS = [_point(1_000, True), _point(2_000, False), _point(9_000, True), _point(8_000, True)]


def test_reliability_diagram_data_two_bins():
    bins = reliability_diagram_data(_POINTS, n_bins=2)

    assert len(bins) == 2
    assert bins[0].count == 2
    assert bins[0].mean_confidence == pytest.approx(0.15)
    assert bins[0].empirical_accuracy == pytest.approx(0.5)
    assert bins[1].count == 2
    assert bins[1].mean_confidence == pytest.approx(0.85)
    assert bins[1].empirical_accuracy == pytest.approx(1.0)


def test_reliability_diagram_data_empty_bin_reports_zero_count():
    bins = reliability_diagram_data([_point(9_000, True)], n_bins=2)

    assert bins[0].count == 0
    assert bins[1].count == 1


def test_expected_calibration_error_matches_hand_computation():
    # ECE = (2*|0.15-0.5| + 2*|0.85-1.0|) / 4 = (0.7 + 0.3) / 4 = 0.25
    assert expected_calibration_error(_POINTS, n_bins=2) == pytest.approx(0.25)


def test_expected_calibration_error_of_no_points_is_zero():
    assert expected_calibration_error([], n_bins=10) == 0.0


def test_brier_score_matches_hand_computation():
    # (0.1-1)^2 + (0.2-0)^2 + (0.9-1)^2 + (0.8-1)^2 = 0.81+0.04+0.01+0.04 = 0.90
    # mean over 4 points = 0.225
    assert brier_score(_POINTS) == pytest.approx(0.225)


def test_brier_score_of_no_points_is_zero():
    assert brier_score([]) == 0.0


def test_brier_score_is_zero_for_perfect_confident_correct_predictions():
    perfect = [_point(10_000, True), _point(0, False)]
    assert brier_score(perfect) == pytest.approx(0.0)


def _entry(records: list[RecordRef]) -> DiscrepancyEntry:
    return DiscrepancyEntry(
        code="D01", discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE, records=records,
        amount_impact_paise=1_000, detail={},
    )


def test_recall_against_ground_truth_matches_hand_computation():
    ground_truth = [_entry([PAY_1]), _entry([PAY_2]), _entry([PAY_3])]
    detected = [PAY_1, PAY_3, PAY_4]  # PAY_2's entry is missed entirely

    assert recall_against_ground_truth(ground_truth, detected) == pytest.approx(2 / 3)


def test_recall_against_ground_truth_of_no_ground_truth_is_one():
    assert recall_against_ground_truth([], [PAY_1]) == 1.0


def test_recall_against_ground_truth_of_nothing_detected_is_zero():
    ground_truth = [_entry([PAY_1])]
    assert recall_against_ground_truth(ground_truth, []) == 0.0
