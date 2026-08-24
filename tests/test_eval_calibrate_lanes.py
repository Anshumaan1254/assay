"""Tests for eval/calibrate_lanes.py's fitting logic, entirely against
small hand-crafted LabeledPoint sets with known correct answers -- zero
dependency on datagen or a real synthetic run (that's
tests/test_eval_calibration.py's job, against the real committed
artifact). A perfectly-separating raw score must calibrate to a step
function, and enough zero-error points must produce an AUTO threshold that
actually achieves the target error rate.
"""

from __future__ import annotations

from core.lanes import SourceKind, calibrate_confidence
from eval.calibrate_lanes import (
    derive_auto_threshold,
    derive_escalate_threshold,
    fit_calibration_artifact,
    fit_isotonic_breakpoints,
)
from eval.labels import LabeledPoint


def _points(raw_bps: int, label: bool, n: int, source=SourceKind.VERIFY_DETERMINISTIC) -> list[LabeledPoint]:
    return [LabeledPoint(source=source, raw_bps=raw_bps, label=label, seed=1) for _ in range(n)]


# ---------------------------------------------------------------------------
# fit_isotonic_breakpoints: a perfectly-separating raw score -> a step function
# ---------------------------------------------------------------------------


def test_perfectly_separating_data_calibrates_to_a_step_function():
    points = _points(0, False, 20) + _points(10_000, True, 20)

    breakpoints = fit_isotonic_breakpoints(points)

    calibration_artifact = _artifact_from_breakpoints(SourceKind.VERIFY_DETERMINISTIC, breakpoints)
    assert calibrate_confidence(0, SourceKind.VERIFY_DETERMINISTIC, calibration_artifact) == 0
    assert calibrate_confidence(10_000, SourceKind.VERIFY_DETERMINISTIC, calibration_artifact) == 10_000


def test_isotonic_breakpoints_are_non_decreasing():
    points = _points(0, False, 5) + _points(3_000, True, 5) + _points(6_000, False, 5) + _points(10_000, True, 5)

    breakpoints = fit_isotonic_breakpoints(points)

    calibrated = [bp.calibrated_bps for bp in breakpoints]
    assert calibrated == sorted(calibrated)


def _artifact_from_breakpoints(source, breakpoints):
    from core.lanes import CalibrationArtifact, LaneThresholds, SourceCalibration, artifact_content_hash

    calibration = SourceCalibration(source=source, breakpoints=breakpoints, n_calibration_points=1, n_positive=1)
    artifact = CalibrationArtifact(
        schema_version=1, fitted_at="2026-08-25T00:00:00+05:30", seeds_used=[1], profile="test",
        calibrations=[calibration],
        thresholds=LaneThresholds(auto_min_calibrated_bps=8_000, escalate_max_calibrated_bps=3_000),
        artifact_sha256="",
    )
    return artifact.model_copy(update={"artifact_sha256": artifact_content_hash(artifact)})


# ---------------------------------------------------------------------------
# derive_auto_threshold: a real coverage guarantee, not a guessed cutoff
# ---------------------------------------------------------------------------


def test_derive_auto_threshold_achieves_the_target_error_rate_with_enough_clean_data():
    # 800 points at calibrated_bps=10_000, zero failures: Clopper-Pearson
    # upper bound at 95% confidence for k=0/n=800 is ~0.37%, comfortably
    # under the 0.5% target -- enough data to certify the guarantee, not
    # just assert it.
    clean = [(10_000, True) for _ in range(800)]
    noisy = [(2_000, False) for _ in range(50)] + [(2_000, True) for _ in range(50)]

    threshold = derive_auto_threshold(clean + noisy, target_error_bps=50, confidence_level_bps=9_500)

    assert threshold <= 10_000
    # Applying the threshold to the noisy population must be excluded --
    # its own Clopper-Pearson bound (50/50 split) cannot possibly satisfy
    # a 0.5% target.
    assert threshold > 2_000


def test_derive_auto_threshold_is_unreachable_when_no_threshold_satisfies_the_target():
    # Every point has a 50% failure rate regardless of threshold -- no
    # amount of data at this error rate can certify a 0.5% guarantee.
    mixed = [(t, label) for t in (0, 5_000, 10_000) for label in (True, False) for _ in range(200)]

    threshold = derive_auto_threshold(mixed, target_error_bps=50, confidence_level_bps=9_500)

    assert threshold > 10_000  # unreachable: AUTO correctly never fires


def test_derive_auto_threshold_of_no_points_is_unreachable():
    assert derive_auto_threshold([], target_error_bps=50, confidence_level_bps=9_500) > 10_000


# ---------------------------------------------------------------------------
# derive_escalate_threshold: below this, a proposal is a coin flip or worse
# ---------------------------------------------------------------------------


def test_derive_escalate_threshold_covers_the_coin_flip_or_worse_region():
    points = [(0, False)] * 10 + [(0, True)] * 10  # 50% accuracy at 0
    points += [(5_000, True)] * 10  # 100% accuracy at 5000

    threshold = derive_escalate_threshold(points)

    assert threshold >= 0
    assert threshold < 5_000


def test_derive_escalate_threshold_of_no_points_is_zero():
    assert derive_escalate_threshold([]) == 0


# ---------------------------------------------------------------------------
# fit_calibration_artifact: end to end over synthetic LabeledPoints
# ---------------------------------------------------------------------------


def test_fit_calibration_artifact_produces_a_valid_hash_verified_artifact():
    from core.lanes import load_calibration

    points_by_source = {
        SourceKind.DECOMPOSE_STRUCTURAL: _points(10_000, True, 100),
        SourceKind.VERIFY_DETERMINISTIC: _points(10_000, True, 100),
    }

    artifact = fit_calibration_artifact(points_by_source, seeds_used=[1, 2, 3], profile="test")

    assert artifact.seeds_used == [1, 2, 3]
    assert artifact.profile == "test"
    assert {c.source for c in artifact.calibrations} == set(points_by_source)
    # round-trips through the same hash-verification load_calibration uses
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        path.write_text(artifact.model_dump_json(), encoding="utf-8")
        loaded = load_calibration(path)
        assert loaded == artifact
