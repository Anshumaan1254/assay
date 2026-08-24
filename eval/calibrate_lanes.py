"""Fits core/lanes.py's calibration artifact from synthetic ground truth.

The only module besides eval/labels.py allowed to read datagen/ ground
truth. Generates seeds via datagen in-memory -- the same mechanism
tests/test_verify.py already uses for its committed-run and clean-profile
tests -- never committing another large generated JSON file, per
DECISIONS.md's precedent. CALIBRATION_SEEDS fits the artifact; TEST_SEEDS
is held out entirely for tests/test_eval_calibration.py to validate the
achieved coverage against.

Two of the three raw sources (decompose.py's per-tier confidence,
verify.py's deterministic recomputation) are pure engine code -- no model
involved. The third, ADJUDICATOR_HYPOTHESIS, needs the adjudicator to have
actually run against real residuals, which means real Gemini calls; the
caller supplies the provider (wrapped in CachedProvider so a first live
pass is the only one that ever queries the network -- every replay after
that hits the committed disk cache, same as llm/contract_parser.py's).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import Random

from scipy.stats import beta as beta_dist
from sklearn.isotonic import IsotonicRegression

from core.conserve import ConservationReport, conserve_all, unclaimed_records
from core.contract import CompiledContract
from core.decompose import DecompositionProof, decompose_all
from core.lanes import (
    CalibrationArtifact,
    IsotonicBreakpoint,
    LaneThresholds,
    SourceCalibration,
    SourceKind,
    artifact_content_hash,
    calibrate_confidence,
)
from core.ledger import Ledger
from core.verify import fee_tax_cells
from datagen.config import GenerationConfig, load_profile
from datagen.ground_truth import DiscrepancyEntry
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card, render_markdown
from datagen.world import build_true_world
from eval.labels import LabeledPoint, label_adjudicated_hypotheses, label_proofs, label_verify_cells
from llm.adjudicator import adjudicate_residuals, residuals_from_run
from llm.contract_parser import compile_rate_card
from llm.provider import LLMProvider
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

CALIBRATION_SEEDS = list(range(1, 15))  # 14 seeds fit the artifact
TEST_SEEDS = list(range(15, 21))  # 6 seeds held out for coverage validation
DEFAULT_PROFILE = "realistic"
DEFAULT_MONTH = "2026-07"
DEFAULT_ARTIFACT_PATH = Path("calibration/lane_calibration.v1.json")

# datagen/world.py's build_true_world and apply_discrepancies each take
# their own Random -- offsetting the injection seed keeps the two streams
# independent (matching tests/test_verify.py's own 42/43 pairing) while
# staying a pure function of `seed`, so a re-fit reproduces the same points.
_INJECTION_SEED_OFFSET = 500_000


@dataclass
class SeedRun:
    seed: int
    ledger: Ledger
    proofs: list[DecompositionProof]
    reports: list[ConservationReport]
    ground_truth: list[DiscrepancyEntry]


def build_reference_contract(*, month: str = DEFAULT_MONTH) -> CompiledContract:
    """The rate card is deterministic per month, independent of seed --
    compiled once and reused across every seed, hitting the committed
    contract-parse cache (no live call for this part)."""
    document = render_markdown(default_rate_card(month))
    return compile_rate_card(document, CachedProvider(NullProvider()))


def generate_seed_run(seed: int, *, profile_name: str = DEFAULT_PROFILE, month: str = DEFAULT_MONTH) -> SeedRun:
    config = GenerationConfig(month=month)
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(seed))
    profile = load_profile(profile_name)
    reported_world, discrepancies, _flags = apply_discrepancies(
        true_world, rate_card, profile, Random(seed + _INJECTION_SEED_OFFSET)
    )

    records = [
        *reported_world.payments, *reported_world.refunds, *reported_world.chargebacks,
        *reported_world.adjustments, *reported_world.fee_lines, *reported_world.tax_lines,
        *reported_world.batches, *reported_world.bank_credits,
    ]
    ledger = Ledger(records)
    proofs = decompose_all(reported_world.bank_credits, ledger, merchant_id=config.merchant_id)
    reports = conserve_all(proofs, ledger)
    return SeedRun(seed=seed, ledger=ledger, proofs=proofs, reports=reports, ground_truth=discrepancies)


def collect_deterministic_points(run: SeedRun, contract: CompiledContract) -> list[LabeledPoint]:
    points = label_proofs(run.proofs, run.ground_truth, run.seed)
    for proof in run.proofs:
        cells = fee_tax_cells(proof, run.ledger, contract)
        points.extend(label_verify_cells(cells, run.ground_truth, run.seed))
    return points


def collect_adjudicator_points(run: SeedRun, provider: LLMProvider) -> list[LabeledPoint]:
    unclaimed = unclaimed_records(run.ledger, run.proofs)
    build = residuals_from_run(run.proofs, run.reports, run.ledger, unclaimed=unclaimed)
    if not build.cases:
        return []
    adjudication = adjudicate_residuals(
        build.cases, run.ledger, provider, skipped_no_evidence=build.skipped_no_evidence
    )
    return label_adjudicated_hypotheses(adjudication, run.ground_truth, run.seed)


# ---------------------------------------------------------------------------
# Fitting: isotonic calibration curves and conformal lane thresholds
# ---------------------------------------------------------------------------


def fit_isotonic_breakpoints(points: Sequence[LabeledPoint]) -> list[IsotonicBreakpoint]:
    """Fit sklearn's isotonic regression on (raw_bps, label) pairs, then
    discretize its fitted step function -- X_thresholds_/y_thresholds_,
    exactly what `.predict()` interpolates between -- into integer-bps
    breakpoints core/lanes.py can apply without floats."""
    if not points:
        raise ValueError("cannot fit a calibration curve with zero points")
    raw = [point.raw_bps for point in points]
    labels = [1.0 if point.label else 0.0 for point in points]
    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    model.fit(raw, labels)

    breakpoints: list[IsotonicBreakpoint] = []
    last_calibrated = 0
    for x, y in zip(model.X_thresholds_, model.y_thresholds_, strict=True):
        raw_bps = max(0, min(10_000, round(float(x))))
        # Rounding two floats to integer bps can occasionally undo the
        # isotonic property by a single bps -- never let it decrease.
        calibrated_bps = max(last_calibrated, max(0, min(10_000, round(float(y) * 10_000))))
        if breakpoints and breakpoints[-1].raw_bps == raw_bps:
            breakpoints[-1] = IsotonicBreakpoint(raw_bps=raw_bps, calibrated_bps=calibrated_bps)
        else:
            breakpoints.append(IsotonicBreakpoint(raw_bps=raw_bps, calibrated_bps=calibrated_bps))
        last_calibrated = calibrated_bps
    return breakpoints


def _clopper_pearson_upper_bound(failures: int, trials: int, confidence_level: float) -> float:
    """One-sided upper confidence bound on a true failure rate, given
    `failures` observed out of `trials`, at `confidence_level`. Exact
    (Beta-distribution-based), not a normal approximation -- valid at any
    sample size, which is the whole reason split conformal prediction can
    state a guarantee at all."""
    if trials == 0 or failures >= trials:
        return 1.0
    return float(beta_dist.ppf(confidence_level, failures + 1, trials - failures))


def derive_auto_threshold(
    calibrated_points: Sequence[tuple[int, bool]], *, target_error_bps: int, confidence_level_bps: int
) -> int:
    """The lowest calibrated-confidence threshold T such that, among
    calibration points with calibrated_confidence >= T, the Clopper-Pearson
    upper bound on the true error rate (at confidence_level_bps) is still
    within target_error_bps. 10_001 (structurally unreachable) if no
    threshold achieves it -- AUTO then correctly never fires rather than
    being silently mis-calibrated."""
    target = target_error_bps / 10_000
    confidence_level = confidence_level_bps / 10_000
    best = 10_001
    for threshold in sorted({calibrated for calibrated, _ in calibrated_points}):
        subset = [label for calibrated, label in calibrated_points if calibrated >= threshold]
        if not subset:
            continue
        failures = sum(1 for label in subset if not label)
        if _clopper_pearson_upper_bound(failures, len(subset), confidence_level) <= target:
            best = min(best, threshold)
    return best


def derive_escalate_threshold(calibrated_points: Sequence[tuple[int, bool]]) -> int:
    """The highest calibrated-confidence value at which empirical accuracy
    is still no better than a coin flip -- below this, a proposal is more
    likely wrong than right, which calls for a human looking directly
    rather than reviewing a suggestion."""
    best = -1
    for threshold in sorted({calibrated for calibrated, _ in calibrated_points}):
        subset = [label for calibrated, label in calibrated_points if calibrated <= threshold]
        if not subset:
            continue
        accuracy = sum(1 for label in subset if label) / len(subset)
        if accuracy <= 0.5:
            best = max(best, threshold)
    return max(best, 0)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def fit_calibration_artifact(
    points_by_source: dict[SourceKind, list[LabeledPoint]],
    *,
    seeds_used: list[int],
    profile: str,
    target_error_bps: int = 50,
    confidence_level_bps: int = 9_500,
) -> CalibrationArtifact:
    """One isotonic calibration per source, then a single pair of lane
    thresholds fit on the pooled calibrated-confidence scale -- pooled
    because calibration is precisely what makes the sources comparable on
    one 0-10_000 axis. is_llm_sourced (core/lanes.py::assign_lane) is what
    keeps an LLM-sourced item out of AUTO regardless of the numeric
    threshold, so pooling ADJUDICATOR_HYPOTHESIS points here does not
    weaken the guarantee for anything that actually reaches AUTO.
    """
    calibrations: list[SourceCalibration] = []
    for source in SourceKind:
        points = points_by_source.get(source, [])
        if not points:
            continue
        calibrations.append(
            SourceCalibration(
                source=source,
                breakpoints=fit_isotonic_breakpoints(points),
                n_calibration_points=len(points),
                n_positive=sum(1 for point in points if point.label),
            )
        )
    if not calibrations:
        raise ValueError("cannot fit a calibration artifact with zero source calibrations")

    interim = CalibrationArtifact(
        schema_version=1,
        fitted_at=_now_iso(),
        seeds_used=seeds_used,
        profile=profile,
        calibrations=calibrations,
        thresholds=LaneThresholds(
            auto_min_calibrated_bps=10_000,
            escalate_max_calibrated_bps=0,
            auto_target_error_bps=target_error_bps,
            auto_confidence_level_bps=confidence_level_bps,
        ),
        artifact_sha256="",
    )

    pooled = [
        (calibrate_confidence(point.raw_bps, source, interim), point.label)
        for source, points in points_by_source.items()
        for point in points
    ]
    auto_min = min(derive_auto_threshold(pooled, target_error_bps=target_error_bps, confidence_level_bps=confidence_level_bps), 10_000)
    escalate_max = derive_escalate_threshold(pooled)

    artifact = interim.model_copy(
        update={
            "thresholds": LaneThresholds(
                auto_min_calibrated_bps=auto_min,
                escalate_max_calibrated_bps=escalate_max,
                auto_target_error_bps=target_error_bps,
                auto_confidence_level_bps=confidence_level_bps,
            )
        }
    )
    return artifact.model_copy(update={"artifact_sha256": artifact_content_hash(artifact)})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fit_from_seeds(
    seeds: Sequence[int],
    provider: LLMProvider,
    *,
    profile_name: str = DEFAULT_PROFILE,
    target_error_bps: int = 50,
    confidence_level_bps: int = 9_500,
) -> CalibrationArtifact:
    contract = build_reference_contract()
    points_by_source: dict[SourceKind, list[LabeledPoint]] = {source: [] for source in SourceKind}
    for seed in seeds:
        run = generate_seed_run(seed, profile_name=profile_name)
        for point in collect_deterministic_points(run, contract):
            points_by_source[point.source].append(point)
        for point in collect_adjudicator_points(run, provider):
            points_by_source[point.source].append(point)
    return fit_calibration_artifact(
        points_by_source,
        seeds_used=list(seeds),
        profile=profile_name,
        target_error_bps=target_error_bps,
        confidence_level_bps=confidence_level_bps,
    )


def write_artifact(artifact: CalibrationArtifact, path: Path | str = DEFAULT_ARTIFACT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")


def main() -> None:
    from llm.providers.gemini import GeminiProvider

    provider = CachedProvider(GeminiProvider())
    artifact = fit_from_seeds(CALIBRATION_SEEDS, provider)
    write_artifact(artifact)
    print(
        f"wrote {DEFAULT_ARTIFACT_PATH}: {len(artifact.calibrations)} source calibrations, "
        f"AUTO >= {artifact.thresholds.auto_min_calibrated_bps} bps, "
        f"ESCALATE <= {artifact.thresholds.escalate_max_calibrated_bps} bps"
    )


if __name__ == "__main__":
    main()
