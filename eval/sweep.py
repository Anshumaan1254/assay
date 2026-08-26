"""The whole evaluation: every profile, every held-out seed, scored and
pooled into one object EVIDENCE.md is rendered from.

Everything here is a measurement. Where a number needs an assumption to
exist at all -- what a token would cost on a paid tier, how fast a human
reconciles a settlement by hand -- the assumption is a named constant with
its source or its reasoning attached, and the rendered document prints the
assumption next to the number it produced. A figure whose assumption is
hidden is not evidence.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from cli.audit import run_audit
from core.lanes import CalibrationArtifact, CalibrationMissing, load_calibration
from eval.ablate import AblationReport, ablate_run, build_report
from eval.detect import DetectionResult, merge
from eval.harness import (
    HELD_OUT_FROM,
    PROFILES,
    SWEEP_SEEDS,
    CalibrationStats,
    GeneratedRun,
    RunResult,
    empty_calibration_stats,
    generate_run,
    score_run,
    specs,
)
from llm.provider import LLMProvider
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from llm.telemetry import ProviderTelemetry

HEADLINE_RUN_DIR = Path("runs/realistic-seed42")

# ---------------------------------------------------------------------------
# Stated assumptions. Each is a number this project does not measure and
# cannot; each is printed in EVIDENCE.md next to whatever it produced.
# ---------------------------------------------------------------------------

# Assumed paid-tier list prices, USD per million tokens. The build itself
# ran entirely on the free tier and paid nothing -- these exist only to
# answer "what would this cost at scale", which is the first question a
# reader asks. Vendor pricing changes; treat these as the figure to update,
# not as a measurement.
ASSUMED_USD_PER_M_INPUT_TOKENS = 0.30
ASSUMED_USD_PER_M_OUTPUT_TOKENS = 2.50

# Assumed manual reconciliation rate for the analyst-hours comparison in
# §11: transactions a person can tie out by hand, per hour, working from a
# settlement report and a bank statement. Deliberately generous to the
# human -- a pessimistic number would flatter the tool.
ASSUMED_MANUAL_TXNS_PER_HOUR = 40


class BoundaryModels(BaseModel):
    contract_parser: str
    adjudicator: str
    narrator: str


class ProviderLimits(BaseModel):
    """The rate-limit configuration a sweep ran under.

    Recorded because §10's "429s observed" and "time spent in backoff" are
    uninterpretable without it: the same quota pressure produces wildly
    different numbers at one retry versus five. Defaulted so an older
    `eval/results/sweep.json` still renders.
    """

    rpm: int = 0
    max_retries: int = 0
    backoff_base_seconds: float = 0.0


class DeterminismCheck(BaseModel):
    run_dir: str
    first_hash: str
    second_hash: str
    replay_hash: str
    matches: bool
    replay_matches: bool
    replay_made_live_calls: int


class ThroughputStats(BaseModel):
    records: int
    payments: int
    wall_clock_seconds: float
    records_per_second: float
    cold_cache_seconds: float | None
    warm_cache_seconds: float | None
    assumed_manual_txns_per_hour: int = ASSUMED_MANUAL_TXNS_PER_HOUR

    @property
    def analyst_hours_equivalent(self) -> float:
        return self.payments / self.assumed_manual_txns_per_hour


class ConformalStats(BaseModel):
    target_error_bps: int
    confidence_level_bps: int
    auto_min_calibrated_bps: int | None
    escalate_max_calibrated_bps: int
    fitted_on_seeds: list[int]
    evaluated_on_seeds: list[int]
    # source -> [count at/above AUTO threshold, of which correct]
    coverage_by_source: dict[str, list[int]]
    lane_counts: dict[str, int]
    lane_paise: dict[str, int]

    @property
    def auto_reachable(self) -> bool:
        return self.auto_min_calibrated_bps is not None


class LLMBoundaryStats(BaseModel):
    telemetry: ProviderTelemetry
    records_total: int
    records_touching_a_model: int
    residuals_submitted: int
    schema_rejections: int
    schema_rejection_opportunities: int
    citations_total: int
    citations_rejected_nonexistent: int
    citations_rejected_not_in_pool: int
    degraded_runs: int

    @property
    def records_touched_fraction(self) -> float:
        return self.records_touching_a_model / self.records_total if self.records_total else 0.0

    @property
    def calls_per_1000_records(self) -> float:
        return self.telemetry.calls * 1000 / self.records_total if self.records_total else 0.0

    @property
    def tokens_per_1000_records(self) -> float:
        return self.telemetry.total_tokens * 1000 / self.records_total if self.records_total else 0.0

    @property
    def unattributed_tokens(self) -> int:
        """Tokens the API bills to a call but attributes to neither prompt
        nor response -- reasoning tokens. Reported rather than dropped: a
        cost estimate that silently ignores billed tokens understates the
        bill, which is the wrong direction to be wrong in."""
        return max(0, self.telemetry.total_tokens - self.telemetry.prompt_tokens - self.telemetry.response_tokens)

    @property
    def assumed_cost_usd(self) -> float:
        """Reasoning tokens are charged at the output rate, which is how
        the vendor bills them and is also the conservative assumption."""
        return (
            self.telemetry.prompt_tokens / 1_000_000 * ASSUMED_USD_PER_M_INPUT_TOKENS
            + (self.telemetry.response_tokens + self.unattributed_tokens)
            / 1_000_000
            * ASSUMED_USD_PER_M_OUTPUT_TOKENS
        )

    @property
    def reference_rejection_rate(self) -> float:
        rejected = self.citations_rejected_nonexistent + self.citations_rejected_not_in_pool
        return rejected / self.citations_total if self.citations_total else 0.0

    @property
    def schema_rejection_rate(self) -> float:
        if not self.schema_rejection_opportunities:
            return 0.0
        return self.schema_rejections / self.schema_rejection_opportunities


class SweepReport(BaseModel):
    # §1 reproduction header
    generated_at: str
    git_commit: str
    python_version: str
    platform: str
    contract_version: str
    calibration_sha256: str | None
    models: BoundaryModels
    limits: ProviderLimits = ProviderLimits()
    headline_run_dir: str
    headline_input_hashes: dict[str, str]
    profiles: list[str]
    seeds: list[int]
    held_out_from_seeds: list[int]
    wall_clock_seconds: float

    runs: list[RunResult]
    detection: DetectionResult
    detection_by_profile: dict[str, DetectionResult]
    calibration: CalibrationStats
    conformal: ConformalStats
    llm: LLMBoundaryStats
    throughput: ThroughputStats
    determinism: DeterminismCheck
    ablation: AblationReport
    clean_profile_unaccounted_paise: int


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        return "unknown"


def _boundary_models() -> BoundaryModels:
    """Read from the environment the same way GeminiConfig does, but
    without requiring an API key -- `make evidence` runs off the committed
    cache and must not need credentials to name the models that produced
    it."""
    import os

    from dotenv import load_dotenv

    from llm.providers.gemini import GeminiConfig

    load_dotenv()
    flash = os.environ.get("GEMINI_MODEL_FLASH", GeminiConfig.model_fields["model_flash"].default)
    flash_lite = os.environ.get(
        "GEMINI_MODEL_FLASH_LITE", GeminiConfig.model_fields["model_flash_lite"].default
    )
    return BoundaryModels(contract_parser=flash, adjudicator=flash, narrator=flash_lite)


def _provider_limits() -> ProviderLimits:
    """Read the same way GeminiConfig reads them, without needing a key."""
    import os

    from dotenv import load_dotenv

    from llm.providers.gemini import GeminiConfig

    load_dotenv()

    def _field(name: str):
        return GeminiConfig.model_fields[name].default

    return ProviderLimits(
        rpm=int(os.environ.get("GEMINI_RPM", _field("rpm"))),
        max_retries=int(os.environ.get("LLM_MAX_RETRIES", _field("max_retries"))),
        backoff_base_seconds=float(
            os.environ.get("LLM_RETRY_BACKOFF_BASE", _field("backoff_base_seconds"))
        ),
    )


def _determinism_check(provider_factory, run_dir: Path = HEADLINE_RUN_DIR) -> DeterminismCheck:
    """Invariant 4, checked rather than argued: audit the same inputs twice
    and compare the hashes, then replay a third time through a provider
    that cannot reach the network at all."""
    first = run_audit(run_dir, provider_factory(), adjudicate=False)
    second = run_audit(run_dir, provider_factory(), adjudicate=False)

    offline = CachedProvider(NullProvider())
    replay = run_audit(run_dir, offline, adjudicate=False)
    from llm.telemetry import snapshot

    return DeterminismCheck(
        # as_posix, not str: this string is printed into a committed
        # document, and a Windows backslash there would make the file
        # differ by the machine that generated it.
        run_dir=Path(run_dir).as_posix(),
        first_hash=first.report_hash,
        second_hash=second.report_hash,
        replay_hash=replay.report_hash,
        matches=first.report_hash == second.report_hash,
        replay_matches=replay.report_hash == first.report_hash,
        replay_made_live_calls=snapshot(offline).live_calls,
    )


def _conformal_stats(
    artifact: CalibrationArtifact | None, calibration: CalibrationStats, runs: Sequence[RunResult]
) -> ConformalStats:
    lane_counts: dict[str, int] = {}
    lane_paise: dict[str, int] = {}
    for run in runs:
        for lane, count in run.lanes.by_lane.items():
            lane_counts[lane] = lane_counts.get(lane, 0) + count
        for lane, paise in run.lanes.paise_by_lane.items():
            lane_paise[lane] = lane_paise.get(lane, 0) + paise

    if artifact is None:
        return ConformalStats(
            target_error_bps=0,
            confidence_level_bps=0,
            auto_min_calibrated_bps=None,
            escalate_max_calibrated_bps=0,
            fitted_on_seeds=[],
            evaluated_on_seeds=[run.seed for run in runs],
            coverage_by_source={},
            lane_counts=lane_counts,
            lane_paise=lane_paise,
        )

    thresholds = artifact.thresholds
    return ConformalStats(
        target_error_bps=thresholds.auto_target_error_bps,
        confidence_level_bps=thresholds.auto_confidence_level_bps,
        auto_min_calibrated_bps=thresholds.auto_min_calibrated_bps,
        escalate_max_calibrated_bps=thresholds.escalate_max_calibrated_bps,
        fitted_on_seeds=list(artifact.seeds_used),
        evaluated_on_seeds=sorted({run.seed for run in runs}),
        coverage_by_source=calibration.by_source,
        lane_counts=lane_counts,
        lane_paise=lane_paise,
    )


def run_sweep(
    provider_factory,
    *,
    profiles: Sequence[str] = PROFILES,
    seeds: Sequence[int] = SWEEP_SEEDS,
    ablate: bool = True,
    progress=None,
) -> SweepReport:
    """Run and score every (profile, seed) combination.

    `provider_factory` is a zero-argument callable rather than a provider,
    because the determinism check needs two independent providers over the
    same cache and reusing one would blur their telemetry together.
    """
    started = time.monotonic()
    try:
        artifact: CalibrationArtifact | None = load_calibration()
    except CalibrationMissing:
        artifact = None

    from cli.loaders import load_contract, load_ledger

    provider = provider_factory()
    results: list[RunResult] = []
    detections: list[DetectionResult] = []
    by_profile: dict[str, list[DetectionResult]] = {}
    per_ablation: list[dict] = []
    calibration = empty_calibration_stats()
    telemetry = ProviderTelemetry()
    schema_rejections = 0
    schema_opportunities = 0
    degraded_runs = 0
    clean_unaccounted = 0

    cold_seconds: float | None = None
    warm_seconds: float | None = None

    for spec in specs(profiles, seeds):
        if progress is not None:
            progress(spec)
        generated: GeneratedRun = generate_run(spec)
        try:
            result, report = score_run(generated, provider, calibration=artifact)
            results.append(result)
            detections.append(result.detection)
            by_profile.setdefault(spec.profile, []).append(result.detection)
            calibration = calibration.merged_with(result.calibration)
            telemetry = telemetry.merged_with(result.llm)

            schema_opportunities += result.llm.calls
            if report.adjudication_degraded:
                degraded_runs += 1
                if report.adjudication_degraded_kind == "schema_rejected":
                    schema_rejections += 1
            if spec.profile == "clean":
                clean_unaccounted += report.total_unaccounted_paise

            elapsed = result.wall_clock_ns / 1_000_000_000
            if result.llm.cache_misses:
                cold_seconds = elapsed if cold_seconds is None else cold_seconds
            else:
                warm_seconds = elapsed if warm_seconds is None else warm_seconds

            if ablate:
                ledger = load_ledger(generated.directory)
                contract = load_contract(generated.directory, provider)
                per_ablation.append(ablate_run(generated, report, ledger, contract, provider))
        finally:
            generated.cleanup()

    pooled = merge(detections)
    records_total = sum(r.record_count for r in results)
    payments_total = sum(r.payment_count for r in results)
    wall_clock = time.monotonic() - started

    return SweepReport(
        generated_at=datetime.now(UTC).isoformat(),
        git_commit=_git_commit(),
        python_version=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        contract_version=results[0].contract_version if results else "unknown",
        calibration_sha256=artifact.artifact_sha256 if artifact else None,
        models=_boundary_models(),
        limits=_provider_limits(),
        headline_run_dir=HEADLINE_RUN_DIR.as_posix(),
        headline_input_hashes=_headline_input_hashes(),
        profiles=list(profiles),
        seeds=list(seeds),
        held_out_from_seeds=list(HELD_OUT_FROM),
        wall_clock_seconds=wall_clock,
        runs=results,
        detection=pooled,
        detection_by_profile={profile: merge(items) for profile, items in sorted(by_profile.items())},
        calibration=calibration,
        conformal=_conformal_stats(artifact, calibration, results),
        llm=LLMBoundaryStats(
            telemetry=telemetry,
            records_total=records_total,
            records_touching_a_model=sum(r.records_touching_a_model for r in results),
            residuals_submitted=sum(r.residuals_submitted for r in results),
            schema_rejections=schema_rejections,
            schema_rejection_opportunities=schema_opportunities,
            citations_total=sum(r.citations_total for r in results),
            citations_rejected_nonexistent=sum(r.citations_rejected_nonexistent for r in results),
            citations_rejected_not_in_pool=sum(r.citations_rejected_not_in_pool for r in results),
            degraded_runs=degraded_runs,
        ),
        throughput=ThroughputStats(
            records=records_total,
            payments=payments_total,
            wall_clock_seconds=sum(r.wall_clock_ns for r in results) / 1_000_000_000,
            records_per_second=(
                records_total / (sum(r.wall_clock_ns for r in results) / 1_000_000_000)
                if records_total
                else 0.0
            ),
            cold_cache_seconds=cold_seconds,
            warm_cache_seconds=warm_seconds,
        ),
        determinism=_determinism_check(provider_factory),
        ablation=build_report(detections, per_ablation) if ablate and per_ablation else _empty_ablation(pooled),
        clean_profile_unaccounted_paise=clean_unaccounted,
    )


def _empty_ablation(baseline: DetectionResult) -> AblationReport:
    return AblationReport(baseline=baseline, rows=[])


def _headline_input_hashes() -> dict[str, str]:
    from cli.audit import hash_inputs

    if not HEADLINE_RUN_DIR.is_dir():
        return {}
    return hash_inputs(HEADLINE_RUN_DIR)


def default_provider_factory() -> LLMProvider:
    """A cached Gemini provider when a key is configured, an offline cached
    NullProvider otherwise. The second is not a fallback that guesses: it
    reads the committed cache and, on a miss, degrades the adjudication
    honestly -- which is exactly what EVIDENCE.md would then report."""
    from llm.provider import ProviderUnavailable

    try:
        from llm.providers.gemini import GeminiProvider

        return CachedProvider(GeminiProvider())
    except ProviderUnavailable:
        return CachedProvider(NullProvider())
