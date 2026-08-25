"""Conformal-calibrated autonomy lanes.

Deterministic and integer-only, on purpose: this file cannot import `llm/`
or `datagen/`, and cannot contain a float literal or a `float()` call
anywhere (tests/test_architecture.py, zero exceptions) -- so the actual
fitting of a calibration curve (split-conformal quantiles, reliability
diagrams, ECE, Brier, isotonic regression) does not happen here. It happens
in `eval/`, the only package allowed to read `datagen/` ground truth and
the only one not bound by the float ban. `eval/calibrate_lanes.py` fits an
isotonic mapping and a pair of lane thresholds against synthetic runs, and
writes a `CalibrationArtifact` -- a committed JSON file, `int`/`str`/enum
fields only. This module loads that artifact and *applies* it, entirely in
integer basis points (0-10_000, matching `Finding.confidence`'s existing
convention), doing nothing itself that a raw-features-only, no-ground-truth
engine module is disallowed from doing.

Every raw confidence this engine produces is a coarse, near-discrete scalar
from one of three sources: `core/decompose.py`'s per-tier confidence
(`CONFIDENCE_BY_TIER`), `core/verify.py`'s always-10_000 deterministic
recomputation confidence, or `llm/adjudicator.py`'s `coverage_bps`. Each is
calibrated independently, via its own isotonic breakpoints in the artifact
-- not combined into one multi-feature model.

`is_llm_sourced` is not a suggestion: `assign_lane` hard-caps any
LLM-sourced item at PROPOSE/ESCALATE regardless of how high its calibrated
confidence lands. This is "it proposes; it never posts" encoded as code,
not left as a convention a caller could forget.
"""

from __future__ import annotations

import json
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from core.contract import canonical_json, sha256_of
from core.models import AssayModel, Lane

if TYPE_CHECKING:
    from core.decompose import DecompositionProof

DEFAULT_CALIBRATION_PATH = Path("calibration/lane_calibration.v1.json")


class CalibrationMissing(Exception):
    """No usable calibration artifact: absent, unreadable, hand-edited
    without recomputing its own hash, or missing a source's calibration.
    Refuses to guess a threshold -- mirrors core.contract.NoApplicableRule.
    """


# ---------------------------------------------------------------------------
# The committed artifact's schema. Lives here (not in eval/) so core/ can
# validate what it loads without importing eval/; eval/calibrate_lanes.py
# imports this same class to construct and write one -- one schema, no
# duplication, mirroring core.contract.RateCardParse.
# ---------------------------------------------------------------------------


class SourceKind(StrEnum):
    DECOMPOSE_STRUCTURAL = "decompose_structural"
    DECOMPOSE_SUBSET_SUM = "decompose_subset_sum"
    DECOMPOSE_ASSIGNMENT = "decompose_assignment"
    VERIFY_DETERMINISTIC = "verify_deterministic"
    ADJUDICATOR_HYPOTHESIS = "adjudicator_hypothesis"


class IsotonicBreakpoint(AssayModel):
    raw_bps: int = Field(ge=0, le=10_000)
    calibrated_bps: int = Field(ge=0, le=10_000)


class SourceCalibration(AssayModel):
    source: SourceKind
    breakpoints: list[IsotonicBreakpoint] = Field(min_length=1)
    n_calibration_points: int
    n_positive: int


class LaneThresholds(AssayModel):
    # None means AUTO is unreachable: no calibrated-confidence value in the
    # fitted data could certify the target error rate, for at least one
    # raw-confidence source. Not a number to be silently clamped into
    # [0, 10_000] -- a clamp would turn "no threshold satisfies the
    # guarantee" into "the threshold is exactly 10_000", which is a
    # different and more permissive claim than the fit actually supports.
    auto_min_calibrated_bps: int | None = Field(default=None, ge=0, le=10_000)
    escalate_max_calibrated_bps: int = Field(ge=0, le=10_000)
    auto_target_error_bps: int = 50  # 0.5%
    auto_confidence_level_bps: int = 9_500  # 95%


class CalibrationArtifact(AssayModel):
    schema_version: int = 1
    fitted_at: str
    seeds_used: list[int]
    profile: str
    calibrations: list[SourceCalibration]
    thresholds: LaneThresholds
    artifact_sha256: str


def artifact_content_hash(artifact: CalibrationArtifact) -> str:
    """The hash `artifact_sha256` must equal: every field except itself,
    canonical-JSON-encoded. Public so eval/calibrate_lanes.py can compute it
    when writing an artifact, and tests can build a valid one by hand --
    the same one-hash-function discipline as core/decompose.py's
    proof_hash and core/contract.py's version_id."""
    payload = artifact.model_dump(mode="json")
    payload.pop("artifact_sha256", None)
    return sha256_of(canonical_json(payload))


def load_calibration(path: Path | str = DEFAULT_CALIBRATION_PATH) -> CalibrationArtifact:
    path = Path(path)
    if not path.is_file():
        raise CalibrationMissing(f"no calibration artifact at {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    artifact = CalibrationArtifact.model_validate(payload)
    if artifact.artifact_sha256 != artifact_content_hash(artifact):
        raise CalibrationMissing(
            f"calibration artifact at {path} failed its own hash check -- "
            "hand-edited or corrupted, refusing to apply it"
        )
    return artifact


def _calibration_for(source: SourceKind, artifact: CalibrationArtifact) -> SourceCalibration:
    for calibration in artifact.calibrations:
        if calibration.source is source:
            return calibration
    raise CalibrationMissing(f"artifact has no calibration for source {source.value!r}")


def calibrate_confidence(raw_bps: int, source: SourceKind, artifact: CalibrationArtifact) -> int:
    """Integer linear interpolation between the source's breakpoints,
    clipped at both ends -- reproduces sklearn's
    `IsotonicRegression(out_of_bounds="clip").predict` behaviour, in
    integer basis points rather than float probabilities."""
    points = _calibration_for(source, artifact).breakpoints
    if raw_bps <= points[0].raw_bps:
        return points[0].calibrated_bps
    if raw_bps >= points[-1].raw_bps:
        return points[-1].calibrated_bps
    for lo, hi in pairwise(points):
        if lo.raw_bps <= raw_bps <= hi.raw_bps:
            if hi.raw_bps == lo.raw_bps:
                return hi.calibrated_bps
            span = hi.raw_bps - lo.raw_bps
            gain = hi.calibrated_bps - lo.calibrated_bps
            return lo.calibrated_bps + gain * (raw_bps - lo.raw_bps) // span
    raise AssertionError(f"{raw_bps} bps did not fall within any breakpoint interval for {source.value!r}")


# ---------------------------------------------------------------------------
# Lane assignment
# ---------------------------------------------------------------------------


class LaneAssignment(AssayModel):
    lane: Lane
    calibrated_confidence_bps: int
    source: SourceKind
    reason: str


def assign_lane(
    raw_confidence_bps: int,
    source: SourceKind,
    *,
    is_llm_sourced: bool,
    artifact: CalibrationArtifact,
) -> LaneAssignment:
    """Calibrate `raw_confidence_bps`, then route it to a lane.

    `is_llm_sourced=True` hard-caps the result at PROPOSE/ESCALATE -- AUTO
    is structurally unreachable for any LLM-sourced item, regardless of
    calibrated confidence. `artifact.thresholds.auto_min_calibrated_bps`
    being `None` has the same effect for every item, LLM-sourced or not:
    the fit could not certify the target error rate at any threshold, so
    AUTO stays unreachable rather than a guess.
    """
    calibrated = calibrate_confidence(raw_confidence_bps, source, artifact)
    thresholds = artifact.thresholds
    auto_min = thresholds.auto_min_calibrated_bps

    if not is_llm_sourced and auto_min is not None and calibrated >= auto_min:
        return LaneAssignment(
            lane=Lane.AUTO,
            calibrated_confidence_bps=calibrated,
            source=source,
            reason=f"calibrated confidence {calibrated} bps at or above the AUTO threshold {auto_min} bps",
        )
    if calibrated <= thresholds.escalate_max_calibrated_bps:
        return LaneAssignment(
            lane=Lane.ESCALATE,
            calibrated_confidence_bps=calibrated,
            source=source,
            reason=f"calibrated confidence {calibrated} bps at or below the ESCALATE threshold "
            f"{thresholds.escalate_max_calibrated_bps} bps",
        )
    if is_llm_sourced:
        reason = "llm-sourced: AUTO is structurally unreachable regardless of calibrated confidence"
    elif auto_min is None:
        reason = f"calibrated confidence {calibrated} bps: AUTO is unreachable, no threshold was certified"
    else:
        reason = f"calibrated confidence {calibrated} bps is between the ESCALATE and AUTO thresholds"
    return LaneAssignment(lane=Lane.PROPOSE, calibrated_confidence_bps=calibrated, source=source, reason=reason)


_TIER_SOURCE = {
    "structural": SourceKind.DECOMPOSE_STRUCTURAL,
    "subset_sum": SourceKind.DECOMPOSE_SUBSET_SUM,
    "assignment": SourceKind.DECOMPOSE_ASSIGNMENT,
}


def assign_lane_for_proof(proof: DecompositionProof, artifact: CalibrationArtifact) -> LaneAssignment:
    source = _TIER_SOURCE[proof.tier.value]
    return assign_lane(proof.confidence, source, is_llm_sourced=False, artifact=artifact)


def assign_lane_for_verify_finding(finding_confidence_bps: int, artifact: CalibrationArtifact) -> LaneAssignment:
    return assign_lane(
        finding_confidence_bps, SourceKind.VERIFY_DETERMINISTIC, is_llm_sourced=False, artifact=artifact
    )


def assign_lane_for_adjudicated_hypothesis(coverage_bps: int, artifact: CalibrationArtifact) -> LaneAssignment:
    return assign_lane(
        coverage_bps, SourceKind.ADJUDICATOR_HYPOTHESIS, is_llm_sourced=True, artifact=artifact
    )
