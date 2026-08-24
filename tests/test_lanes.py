"""Tests for core/lanes.py.

Money- and trust-adjacent: this is the module that decides which findings
are allowed to post without a human (AUTO) versus which need one
(PROPOSE/ESCALATE). Every test here works against a hand-built
CalibrationArtifact fixture -- zero dependency on the real eval/ fitting
pipeline, mirroring how tests/test_contract_parser.py hand-builds
good_response() rather than running a real LLM. The eval/ harness that
actually FITS an artifact from ground truth gets its own tests separately.

The single most important test in this file is
test_llm_sourced_can_never_reach_auto: "it proposes, it never posts" has to
be true even when the arithmetic says the model was completely right.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.decompose import DecompositionTier
from core.lanes import (
    CalibrationArtifact,
    CalibrationMissing,
    IsotonicBreakpoint,
    LaneThresholds,
    SourceCalibration,
    SourceKind,
    artifact_content_hash,
    assign_lane,
    assign_lane_for_adjudicated_hypothesis,
    assign_lane_for_proof,
    assign_lane_for_verify_finding,
    calibrate_confidence,
    load_calibration,
)
from core.models import Lane

_DEFAULT_BREAKPOINTS = [
    IsotonicBreakpoint(raw_bps=0, calibrated_bps=0),
    IsotonicBreakpoint(raw_bps=5_000, calibrated_bps=4_000),
    IsotonicBreakpoint(raw_bps=10_000, calibrated_bps=9_000),
]


def _artifact(
    breakpoints: list[IsotonicBreakpoint] | None = None,
    auto_min: int = 8_000,
    escalate_max: int = 3_000,
    sources: list[SourceKind] | None = None,
) -> CalibrationArtifact:
    breakpoints = breakpoints if breakpoints is not None else _DEFAULT_BREAKPOINTS
    sources = sources if sources is not None else list(SourceKind)
    calibrations = [
        SourceCalibration(source=source, breakpoints=breakpoints, n_calibration_points=1_000, n_positive=900)
        for source in sources
    ]
    artifact = CalibrationArtifact(
        schema_version=1,
        fitted_at="2026-08-25T00:00:00+05:30",
        seeds_used=[1, 2, 3],
        profile="realistic",
        calibrations=calibrations,
        thresholds=LaneThresholds(
            auto_min_calibrated_bps=auto_min,
            escalate_max_calibrated_bps=escalate_max,
            auto_target_error_bps=50,
            auto_confidence_level_bps=9_500,
        ),
        artifact_sha256="",
    )
    return artifact.model_copy(update={"artifact_sha256": artifact_content_hash(artifact)})


# ---------------------------------------------------------------------------
# load_calibration: round-trip, missing file, tampered content
# ---------------------------------------------------------------------------


def test_round_trip_load(tmp_path: Path):
    artifact = _artifact()
    path = tmp_path / "lane_calibration.v1.json"
    path.write_text(artifact.model_dump_json(), encoding="utf-8")

    loaded = load_calibration(path)

    assert loaded == artifact


def test_calibration_missing_when_the_file_does_not_exist(tmp_path: Path):
    with pytest.raises(CalibrationMissing):
        load_calibration(tmp_path / "does_not_exist.json")


def test_calibration_missing_when_the_content_was_hand_edited(tmp_path: Path):
    artifact = _artifact()
    path = tmp_path / "lane_calibration.v1.json"
    tampered = json.loads(artifact.model_dump_json())
    tampered["thresholds"]["auto_min_calibrated_bps"] = 1  # edited without recomputing the hash
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(CalibrationMissing):
        load_calibration(path)


# ---------------------------------------------------------------------------
# calibrate_confidence: integer interpolation, no floats anywhere
# ---------------------------------------------------------------------------


def test_calibrate_confidence_exact_breakpoint_values():
    artifact = _artifact()
    assert calibrate_confidence(0, SourceKind.VERIFY_DETERMINISTIC, artifact) == 0
    assert calibrate_confidence(5_000, SourceKind.VERIFY_DETERMINISTIC, artifact) == 4_000
    assert calibrate_confidence(10_000, SourceKind.VERIFY_DETERMINISTIC, artifact) == 9_000


def test_calibrate_confidence_interpolates_between_breakpoints():
    artifact = _artifact()
    # halfway between (0,0) and (5000,4000): 0 + 4000 * 2500 // 5000 = 2000
    assert calibrate_confidence(2_500, SourceKind.VERIFY_DETERMINISTIC, artifact) == 2_000


def test_calibrate_confidence_clips_below_the_first_breakpoint():
    breakpoints = [IsotonicBreakpoint(raw_bps=2_000, calibrated_bps=1_000), IsotonicBreakpoint(raw_bps=10_000, calibrated_bps=9_000)]
    artifact = _artifact(breakpoints=breakpoints)
    assert calibrate_confidence(0, SourceKind.VERIFY_DETERMINISTIC, artifact) == 1_000


def test_calibrate_confidence_clips_above_the_last_breakpoint():
    breakpoints = [IsotonicBreakpoint(raw_bps=0, calibrated_bps=0), IsotonicBreakpoint(raw_bps=8_000, calibrated_bps=7_000)]
    artifact = _artifact(breakpoints=breakpoints)
    assert calibrate_confidence(10_000, SourceKind.VERIFY_DETERMINISTIC, artifact) == 7_000


def test_calibrate_confidence_is_monotonic_non_decreasing():
    artifact = _artifact()
    samples = [0, 1_000, 2_500, 4_999, 5_000, 5_001, 7_500, 9_999, 10_000]
    calibrated = [calibrate_confidence(x, SourceKind.VERIFY_DETERMINISTIC, artifact) for x in samples]
    assert calibrated == sorted(calibrated)


def test_calibrate_confidence_raises_for_a_source_with_no_calibration():
    artifact = _artifact(sources=[SourceKind.VERIFY_DETERMINISTIC])  # ADJUDICATOR_HYPOTHESIS missing
    with pytest.raises(CalibrationMissing):
        calibrate_confidence(5_000, SourceKind.ADJUDICATOR_HYPOTHESIS, artifact)


# ---------------------------------------------------------------------------
# assign_lane: the three boundaries
# ---------------------------------------------------------------------------


def test_high_calibrated_confidence_routes_to_auto():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane(10_000, SourceKind.VERIFY_DETERMINISTIC, is_llm_sourced=False, artifact=artifact)
    assert result.lane is Lane.AUTO
    assert result.calibrated_confidence_bps == 9_000


def test_low_calibrated_confidence_routes_to_escalate():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane(0, SourceKind.VERIFY_DETERMINISTIC, is_llm_sourced=False, artifact=artifact)
    assert result.lane is Lane.ESCALATE
    assert result.calibrated_confidence_bps == 0


def test_mid_calibrated_confidence_routes_to_propose():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane(5_000, SourceKind.VERIFY_DETERMINISTIC, is_llm_sourced=False, artifact=artifact)
    assert result.lane is Lane.PROPOSE
    assert result.calibrated_confidence_bps == 4_000


# ---------------------------------------------------------------------------
# The single most important test: LLM-sourced can never reach AUTO
# ---------------------------------------------------------------------------


def test_llm_sourced_can_never_reach_auto():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)

    result = assign_lane(10_000, SourceKind.ADJUDICATOR_HYPOTHESIS, is_llm_sourced=True, artifact=artifact)

    assert result.lane is not Lane.AUTO
    assert result.calibrated_confidence_bps == 9_000  # calibration still ran; only the lane cap differs


def test_llm_sourced_low_confidence_still_escalates():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane(0, SourceKind.ADJUDICATOR_HYPOTHESIS, is_llm_sourced=True, artifact=artifact)
    assert result.lane is Lane.ESCALATE


# ---------------------------------------------------------------------------
# assign_lane_for_* wrappers
# ---------------------------------------------------------------------------


def test_assign_lane_for_proof_maps_tier_to_source_and_is_never_llm_sourced():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)

    class _FakeProof:
        tier = DecompositionTier.STRUCTURAL
        confidence = 10_000

    result = assign_lane_for_proof(_FakeProof(), artifact)
    assert result.source is SourceKind.DECOMPOSE_STRUCTURAL
    assert result.lane is Lane.AUTO  # non-LLM-sourced, so AUTO is reachable


def test_assign_lane_for_verify_finding_uses_the_verify_deterministic_source():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane_for_verify_finding(10_000, artifact)
    assert result.source is SourceKind.VERIFY_DETERMINISTIC
    assert result.lane is Lane.AUTO


def test_assign_lane_for_adjudicated_hypothesis_can_never_reach_auto():
    artifact = _artifact(auto_min=8_000, escalate_max=3_000)
    result = assign_lane_for_adjudicated_hypothesis(10_000, artifact)
    assert result.source is SourceKind.ADJUDICATOR_HYPOTHESIS
    assert result.lane is not Lane.AUTO
