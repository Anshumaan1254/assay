"""One synthetic month in, one scored audit out -- repeated across every
profile and held-out seed.

The harness deliberately runs the **product path**, not a reimplementation
of it: each run is materialised to four real input files and handed to
`cli.audit.run_audit`, exactly as `assay audit` would. That costs a few
seconds of JSON per run and buys three things worth more than the seconds:
the input hashes in EVIDENCE.md are hashes of real files, §11's throughput
number includes the I/O a real audit actually does, and no second
orchestration exists to drift out of step with the first.

Seeds 15-20 are used and seeds 1-14 are not. That split is not arbitrary:
`eval/calibrate_lanes.py` fits `calibration/lane_calibration.v1.json` on
CALIBRATION_SEEDS (1-14) and reserves TEST_SEEDS (15-20). Scoring
calibration on the seeds that fit it would make §8 and §9 report how well
the artifact memorised its own training data. This module imports those two
constants rather than restating the numbers, so the split cannot drift.

The rate card is identical across every profile and seed (it is a pure
function of the month), so all 18 runs hit the same committed
contract-parse cache entry and none of them costs a live call for it. The
only live calls a first pass ever makes are adjudication.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from random import Random
from tempfile import mkdtemp

from pydantic import BaseModel

from cli.audit import AuditReport, run_audit
from core.contract import CompiledContract
from core.decompose import DecompositionOutcome, DecompositionTier
from core.lanes import CalibrationArtifact, calibrate_confidence
from core.ledger import Ledger
from core.models import IST, EntityType, Lane
from core.verify import fee_tax_cells
from datagen.config import GenerationConfig, load_profile
from datagen.ground_truth import GroundTruth
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from eval.calibrate_lanes import CALIBRATION_SEEDS, INJECTION_SEED_OFFSET, TEST_SEEDS
from eval.detect import DetectionResult, evaluate
from eval.labels import LabeledPoint, label_adjudicated_hypotheses, label_proofs, label_verify_cells
from llm.adjudicator import residuals_from_run
from llm.provider import LLMProvider
from llm.telemetry import ProviderTelemetry, reset, snapshot

PROFILES = ("clean", "realistic", "stress")
DEFAULT_MONTH = "2026-07"

# Re-exported so eval/cli.py and the evidence renderer name one thing.
SWEEP_SEEDS = tuple(TEST_SEEDS)
HELD_OUT_FROM = tuple(CALIBRATION_SEEDS)


@dataclass
class RunSpec:
    profile: str
    seed: int
    month: str = DEFAULT_MONTH

    @property
    def run_id(self) -> str:
        return f"{self.profile}-seed{self.seed}"


# ---------------------------------------------------------------------------
# Aggregatable calibration statistics
#
# A single realistic run produces ~170,000 verify cells. Eighteen runs of
# raw LabeledPoints would be a multi-hundred-megabyte results file for
# numbers that are only ever read in aggregate, so points are folded into
# bin counters here and only the counters are persisted. Everything §8
# reports -- reliability curve, ECE, Brier -- is recoverable from them.
# ---------------------------------------------------------------------------

N_BINS = 10
_BIN_WIDTH_BPS = 10_000 // N_BINS


class ConfidenceBin(BaseModel):
    bin_lo_bps: int
    bin_hi_bps: int
    count: int = 0
    sum_confidence_bps: int = 0
    positives: int = 0
    sum_squared_error: float = 0.0

    @property
    def mean_confidence(self) -> float:
        return (self.sum_confidence_bps / self.count / 10_000) if self.count else 0.0

    @property
    def empirical_accuracy(self) -> float:
        return (self.positives / self.count) if self.count else 0.0


class CalibrationStats(BaseModel):
    """Bin counters plus the per-source detail §9 needs."""

    bins: list[ConfidenceBin]
    by_source: dict[str, list[int]] = {}  # source -> [count, positives] at/above the AUTO threshold
    source_totals: dict[str, list[int]] = {}  # source -> [count, positives] overall

    @property
    def total(self) -> int:
        return sum(b.count for b in self.bins)

    @property
    def expected_calibration_error(self) -> float:
        total = self.total
        if not total:
            return 0.0
        return sum(b.count * abs(b.mean_confidence - b.empirical_accuracy) for b in self.bins) / total

    @property
    def brier_score(self) -> float:
        total = self.total
        return sum(b.sum_squared_error for b in self.bins) / total if total else 0.0

    def merged_with(self, other: CalibrationStats) -> CalibrationStats:
        bins = [
            ConfidenceBin(
                bin_lo_bps=left.bin_lo_bps,
                bin_hi_bps=left.bin_hi_bps,
                count=left.count + right.count,
                sum_confidence_bps=left.sum_confidence_bps + right.sum_confidence_bps,
                positives=left.positives + right.positives,
                sum_squared_error=left.sum_squared_error + right.sum_squared_error,
            )
            for left, right in zip(self.bins, other.bins, strict=True)
        ]
        return CalibrationStats(
            bins=bins,
            by_source=_merge_pairs(self.by_source, other.by_source),
            source_totals=_merge_pairs(self.source_totals, other.source_totals),
        )


def _merge_pairs(left: dict[str, list[int]], right: dict[str, list[int]]) -> dict[str, list[int]]:
    merged: dict[str, list[int]] = {}
    for key in sorted(set(left) | set(right)):
        a = left.get(key, [0, 0])
        b = right.get(key, [0, 0])
        merged[key] = [a[0] + b[0], a[1] + b[1]]
    return merged


def empty_calibration_stats() -> CalibrationStats:
    return CalibrationStats(
        bins=[
            ConfidenceBin(bin_lo_bps=i * _BIN_WIDTH_BPS, bin_hi_bps=(i + 1) * _BIN_WIDTH_BPS)
            for i in range(N_BINS)
        ]
    )


def fold_points(
    points: Sequence[LabeledPoint], artifact: CalibrationArtifact | None
) -> CalibrationStats:
    """Bin raw confidences, and -- when an artifact is available -- record
    how each source behaves at and above the AUTO threshold. That second
    part is §9's empirical coverage: the fraction of items the conformal
    rule would have auto-posted that were actually right."""
    stats = empty_calibration_stats()
    auto_min = artifact.thresholds.auto_min_calibrated_bps if artifact is not None else None

    for point in points:
        index = min(point.raw_bps // _BIN_WIDTH_BPS, N_BINS - 1)
        target = stats.bins[index]
        target.count += 1
        target.sum_confidence_bps += point.raw_bps
        target.positives += 1 if point.label else 0
        target.sum_squared_error += ((point.raw_bps / 10_000) - (1.0 if point.label else 0.0)) ** 2

        source = point.source.value
        totals = stats.source_totals.setdefault(source, [0, 0])
        totals[0] += 1
        totals[1] += 1 if point.label else 0

        if artifact is None or auto_min is None:
            continue
        calibrated = calibrate_confidence(point.raw_bps, point.source, artifact)
        if calibrated >= auto_min:
            above = stats.by_source.setdefault(source, [0, 0])
            above[0] += 1
            above[1] += 1 if point.label else 0
    return stats


# ---------------------------------------------------------------------------
# Per-run result
# ---------------------------------------------------------------------------


class DecompositionStats(BaseModel):
    credits: int
    by_tier: dict[str, int]
    resolved_by_tier: dict[str, int]
    ambiguous: int
    unresolved: int
    unresolved_reasons: dict[str, int]

    @property
    def resolved(self) -> int:
        return sum(self.resolved_by_tier.values())


class LaneStats(BaseModel):
    by_lane: dict[str, int]
    paise_by_lane: dict[str, int]


class ConservationStats(BaseModel):
    total_unexplained_paise: int
    unclaimed_paise: int
    total_unaccounted_paise: int
    settled_gross_paise: int

    @property
    def unexplained_bps_of_volume(self) -> float:
        if self.settled_gross_paise == 0:
            return 0.0
        return abs(self.total_unaccounted_paise) * 10_000 / self.settled_gross_paise


class RunResult(BaseModel):
    profile: str
    seed: int
    run_id: str

    input_hashes: dict[str, str]
    input_hash: str
    report_hash: str
    contract_version: str
    calibration_sha256: str | None

    record_count: int
    payment_count: int

    detection: DetectionResult
    decomposition: DecompositionStats
    conservation: ConservationStats
    lanes: LaneStats
    calibration: CalibrationStats
    llm: ProviderTelemetry

    findings_total: int
    clusters_total: int
    adjudication_degraded: bool
    adjudication_degraded_reason: str | None
    residuals_submitted: int
    adjudication_api_calls: int
    citations_total: int
    citations_rejected_nonexistent: int
    citations_rejected_not_in_pool: int
    records_touching_a_model: int

    wall_clock_ns: int

    @property
    def records_per_second(self) -> float:
        seconds = self.wall_clock_ns / 1_000_000_000
        return self.record_count / seconds if seconds else 0.0


@dataclass
class GeneratedRun:
    """A materialised run directory plus the truth about what is in it."""

    spec: RunSpec
    directory: Path
    ground_truth: GroundTruth
    merchant_id: str
    # Set only when this run owns a temp directory it is responsible for
    # removing. None when the caller supplied the location and owns it.
    _parent: Path | None = field(repr=False, default=None)

    def cleanup(self) -> None:
        if self._parent is not None and self._parent.exists():
            shutil.rmtree(self._parent, ignore_errors=True)


def generate_run(spec: RunSpec, into: Path | None = None) -> GeneratedRun:
    """Generate one profile/seed month and write it out as the four input
    files an audit consumes. Ground truth is returned in memory and never
    written next to those inputs -- invariant 5's quarantine is a directory
    boundary as much as an import one."""
    config = GenerationConfig(month=spec.month)
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(spec.seed))
    profile = load_profile(spec.profile)
    reported_world, discrepancies, flags = apply_discrepancies(
        true_world, rate_card, profile, Random(spec.seed + INJECTION_SEED_OFFSET)
    )

    parent = Path(into) if into is not None else Path(mkdtemp(prefix="assay-eval-"))
    directory = parent / spec.run_id
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": spec.run_id, "seed": spec.seed, "profile": spec.profile, "month": spec.month},
        out_dir=directory,
        merchant_id=config.merchant_id,
    )

    # A fixed instant, not now(): this GroundTruth is scored in memory and
    # never written, and a wall-clock stamp inside it would be one more
    # thing that differs between two otherwise identical sweeps.
    year, month_number = (int(part) for part in spec.month.split("-"))
    ground_truth = GroundTruth(
        run_id=spec.run_id,
        seed=spec.seed,
        profile=spec.profile,
        generated_at=datetime(year, month_number, 1, tzinfo=IST),
        discrepancies=discrepancies,
        data_quality_flags=flags,
        counts=profile.model_dump(),
    )
    return GeneratedRun(
        spec=spec,
        directory=directory,
        ground_truth=ground_truth,
        merchant_id=config.merchant_id,
        _parent=parent if into is None else None,
    )


def _decomposition_stats(report: AuditReport) -> DecompositionStats:
    by_tier: dict[str, int] = {tier.value: 0 for tier in DecompositionTier}
    resolved_by_tier: dict[str, int] = {tier.value: 0 for tier in DecompositionTier}
    unresolved_reasons: dict[str, int] = {}
    ambiguous = unresolved = 0

    for proof in report.proofs:
        by_tier[proof.tier.value] += 1
        if proof.outcome is DecompositionOutcome.RESOLVED:
            resolved_by_tier[proof.tier.value] += 1
        elif proof.outcome is DecompositionOutcome.AMBIGUOUS:
            ambiguous += 1
        else:
            unresolved += 1
        if proof.outcome is not DecompositionOutcome.RESOLVED and proof.reason is not None:
            unresolved_reasons[proof.reason.value] = unresolved_reasons.get(proof.reason.value, 0) + 1

    return DecompositionStats(
        credits=len(report.proofs),
        by_tier=by_tier,
        resolved_by_tier=resolved_by_tier,
        ambiguous=ambiguous,
        unresolved=unresolved,
        unresolved_reasons=dict(sorted(unresolved_reasons.items())),
    )


def _lane_stats(report: AuditReport) -> LaneStats:
    by_lane = {lane.value: 0 for lane in Lane}
    paise_by_lane = {lane.value: 0 for lane in Lane}
    for finding in report.findings:
        by_lane[finding.lane.value] += 1
        paise_by_lane[finding.lane.value] += abs(finding.amount_impact.paise)
    return LaneStats(by_lane=by_lane, paise_by_lane=paise_by_lane)


def _settled_gross_paise(report: AuditReport) -> int:
    return sum(c.settled_gross_paise for c in report.conservation)


def _records_touching_a_model(report: AuditReport, ledger: Ledger) -> int:
    """How many ledger records were shown to a model at all.

    Recomputed here from already-computed pipeline output rather than
    threaded through the audit: `residuals_from_run` is pure reshaping with
    no model call, so asking it twice costs nothing and keeps
    `AdjudicationRun` from growing a field only the eval harness reads.
    """
    if report.adjudication is None:
        return 0
    build = residuals_from_run(report.proofs, report.conservation, ledger, unclaimed=report.unclaimed)
    submitted = {result.residual_id for result in report.adjudication.results}
    shown = {ref for case in build.cases if case.residual_id in submitted for ref in case.evidence_pool}
    return len(shown)


def _collect_points(
    report: AuditReport, ledger: Ledger, contract: CompiledContract, ground_truth: GroundTruth, seed: int
) -> list[LabeledPoint]:
    truth = ground_truth.discrepancies
    points = label_proofs(report.proofs, truth, seed)
    for proof in report.proofs:
        points.extend(label_verify_cells(fee_tax_cells(proof, ledger, contract), truth, seed))
    if report.adjudication is not None:
        points.extend(label_adjudicated_hypotheses(report.adjudication, truth, seed))
    return points


def score_run(
    generated: GeneratedRun,
    provider: LLMProvider,
    *,
    calibration: CalibrationArtifact | None = None,
    adjudicate: bool = True,
) -> tuple[RunResult, AuditReport]:
    """Audit one generated run and score it against its own ground truth."""
    from cli.loaders import load_contract, load_ledger

    reset(provider)
    started = time.monotonic_ns()
    # Compiled once and handed to run_audit, rather than letting run_audit
    # compile its own and then compiling a second one for the labels. Two
    # parses would be two provider calls for one audit, and §10 reports
    # calls per 1,000 records -- a number the measurement apparatus must
    # not inflate on its own.
    contract = load_contract(generated.directory, provider)
    report = run_audit(
        generated.directory,
        provider,
        merchant_id=generated.merchant_id,
        calibration=calibration,
        contract=contract,
        adjudicate=adjudicate,
    )
    elapsed = time.monotonic_ns() - started

    ledger = load_ledger(generated.directory)

    detection = evaluate(report.findings, generated.ground_truth)
    points = _collect_points(report, ledger, contract, generated.ground_truth, generated.spec.seed)
    adjudication = report.adjudication

    result = RunResult(
        profile=generated.spec.profile,
        seed=generated.spec.seed,
        run_id=generated.spec.run_id,
        input_hashes=report.input_hashes,
        input_hash=report.input_hash,
        report_hash=report.report_hash,
        contract_version=report.contract_version,
        calibration_sha256=report.calibration_sha256,
        record_count=report.record_count,
        payment_count=len(ledger.by_type(EntityType.PAYMENT)),
        detection=detection,
        decomposition=_decomposition_stats(report),
        conservation=ConservationStats(
            total_unexplained_paise=report.total_unexplained_paise,
            unclaimed_paise=report.unclaimed_paise,
            total_unaccounted_paise=report.total_unaccounted_paise,
            settled_gross_paise=_settled_gross_paise(report),
        ),
        lanes=_lane_stats(report),
        calibration=fold_points(points, calibration),
        llm=snapshot(provider),
        findings_total=len(report.findings),
        clusters_total=len(report.clusters),
        adjudication_degraded=report.adjudication_degraded,
        adjudication_degraded_reason=report.adjudication_degraded_reason,
        residuals_submitted=adjudication.residuals_submitted if adjudication else 0,
        adjudication_api_calls=adjudication.api_call_count if adjudication else 0,
        citations_total=adjudication.citations_total if adjudication else 0,
        citations_rejected_nonexistent=adjudication.citations_rejected_nonexistent if adjudication else 0,
        citations_rejected_not_in_pool=adjudication.citations_rejected_not_in_pool if adjudication else 0,
        records_touching_a_model=_records_touching_a_model(report, ledger),
        wall_clock_ns=elapsed,
    )
    return result, report


def specs(profiles: Sequence[str] = PROFILES, seeds: Sequence[int] = SWEEP_SEEDS) -> list[RunSpec]:
    return [RunSpec(profile=profile, seed=seed) for profile in profiles for seed in seeds]
