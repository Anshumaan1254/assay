"""Does the AI actually earn its place. In numbers.

Four components are disabled one at a time and the sweep is re-scored, so
each row of §13 reads "with this, X; without it, Y". Two deltas are
reported for each: value-weighted recall (what removing it costs in rupees
found) and false-positive rupees (what removing it costs in rupees wrongly
claimed). A component that improves one while wrecking the other has not
earned anything, and the table is built to show that rather than hide it.

**None of the four ablations required a change to `core/`.** That is worth
stating, because the obvious implementation of the third one -- a
`max_tier` parameter threaded into `decompose_all` -- would have put an
eval-only switch on the money path, and a money path grows switches badly.

  1. *No adjudicator.* Score only what `core/verify.py` produced. The
     adjudicator's findings are already tagged by id prefix, so this is a
     filter.
  2. *No conformal calibration.* Re-assign lanes by the fixed rule a
     pre-calibration system would use -- raw confidence >= 9,000 becomes
     AUTO -- and report what that would have auto-posted. Computed here,
     never in `core/lanes.py`.
  3. *No tier 2 or 3 decomposition.* Keep only proofs that resolved
     structurally, and re-run verify and conserve over exactly those.
     This is *equivalent* to a structural-only run, not an approximation
     of one: `decompose_all` completes its structural phase over EVERY
     credit before any credit is allowed to fall to tier 2 (the
     2026-08-26 00:21 fix in DECISIONS.md), so a structural result never
     depends on what the later tiers went on to do. Filtering therefore
     reproduces the run tier 1 alone would have produced.
  4. *No reference checker.* Re-adjudicate with
     `check_references=False`, which is what invariant 6 is worth in
     rupees -- and in fabricated record ids that would have reached a
     report.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from core.conserve import conserve_all
from core.contract import CompiledContract
from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier
from core.ledger import Ledger
from core.models import Finding, Lane
from core.verify import verify_all
from eval.detect import DetectionResult, evaluate, merge
from eval.harness import GeneratedRun
from llm.adjudicator import (
    AdjudicationRejected,
    adjudicate_residuals,
    findings_from_adjudication_run,
    residuals_from_run,
)
from llm.provider import LLMProvider, ProviderUnavailable

# What a system with no calibration would have to use instead: a round
# number somebody picked. 0.9 is the conventional one, which is the point.
FIXED_AUTO_THRESHOLD_BPS = 9_000

ADJUDICATED_FINDING_PREFIX = "FND-ADJ-"


class AblationRow(BaseModel):
    name: str
    # What the ablated configuration IS, phrased so it can stand alone in a
    # table cell. `name` alone cannot: "without no_adjudicator" is a double
    # negative, and "without structural_only" is not even wrong.
    label: str
    description: str
    detection: DetectionResult
    # Populated only where the ablation changes something a detection
    # result cannot express (lanes, fabricated citations).
    notes: dict[str, int] = {}

    @property
    def value_recall(self) -> float:
        return self.detection.value_recall

    @property
    def false_positive_paise(self) -> int:
        return self.detection.false_positive_paise


class AblationReport(BaseModel):
    baseline: DetectionResult
    baseline_notes: dict[str, int] = {}
    rows: list[AblationRow]

    def delta_value_recall(self, row: AblationRow) -> float:
        return row.value_recall - self.baseline.value_recall

    def delta_false_positive_paise(self, row: AblationRow) -> int:
        return row.false_positive_paise - self.baseline.false_positive_paise


# ---------------------------------------------------------------------------
# 1. Without the LLM adjudicator
# ---------------------------------------------------------------------------


def without_adjudicator(findings: Sequence[Finding]) -> list[Finding]:
    return [f for f in findings if not f.id.startswith(ADJUDICATED_FINDING_PREFIX)]


# ---------------------------------------------------------------------------
# 2. Without conformal calibration
# ---------------------------------------------------------------------------


def with_fixed_threshold(findings: Sequence[Finding]) -> list[Finding]:
    """Lanes as a pre-calibration system would assign them.

    Note what this does NOT do: it does not exempt LLM-sourced findings
    from AUTO. That exemption is a consequence of having calibrated the
    sources separately and discovered they are not comparable; a system
    with one fixed threshold has no basis for it. Reproducing the
    exemption here would flatter the ablation by lending it a safeguard
    the thing being ablated is what produced.
    """
    return [
        f.model_copy(
            update={"lane": Lane.AUTO if f.confidence >= FIXED_AUTO_THRESHOLD_BPS else Lane.PROPOSE}
        )
        for f in findings
    ]


# ---------------------------------------------------------------------------
# 3. Without tier 2 and tier 3 decomposition
# ---------------------------------------------------------------------------


def structural_only(proofs: Sequence[DecompositionProof]) -> list[DecompositionProof]:
    return [
        p
        for p in proofs
        if p.tier is DecompositionTier.STRUCTURAL and p.outcome is DecompositionOutcome.RESOLVED
    ]


def structural_only_findings(
    proofs: Sequence[DecompositionProof], ledger: Ledger, contract: CompiledContract, audit_run_id: str
) -> list[Finding]:
    kept = structural_only(proofs)
    return list(verify_all(kept, ledger, contract, audit_run_id=audit_run_id))


def structural_only_unexplained_paise(
    proofs: Sequence[DecompositionProof], ledger: Ledger
) -> int:
    """What a structural-only run would leave unexplained: the residual on
    the credits tier 1 did resolve, plus the FULL amount of every credit it
    did not. A credit no tier resolved is not zero unexplained money -- it
    is entirely unexplained money, and an ablation that forgot the second
    term would make tier 1 look far better than it is."""
    kept = structural_only(proofs)
    kept_refs = {p.credit_ref for p in kept}
    resolved_residual = sum(r.unexplained_paise for r in conserve_all(kept, ledger))
    dropped = sum(p.credit_paise for p in proofs if p.credit_ref not in kept_refs)
    return resolved_residual + dropped


# ---------------------------------------------------------------------------
# 4. Without the reference checker
# ---------------------------------------------------------------------------


def unchecked_adjudication_findings(
    proofs: Sequence[DecompositionProof],
    conservation: Sequence,
    ledger: Ledger,
    unclaimed: Sequence,
    provider: LLMProvider,
    audit_run_id: str,
) -> tuple[list[Finding], int]:
    """Re-adjudicate trusting every citation. Returns the findings plus the
    count of fabricated record ids that reached them.

    Every call here is a cache hit in a replay: the prompt is byte-identical
    to the one the real audit sent, and only the post-processing differs.
    So this ablation costs no extra API calls at all.
    """
    build = residuals_from_run(proofs, conservation, ledger, unclaimed=unclaimed)
    if not build.cases:
        return [], 0
    try:
        run = adjudicate_residuals(
            build.cases,
            ledger,
            provider,
            skipped_no_evidence=build.skipped_no_evidence,
            check_references=False,
        )
    except (ProviderUnavailable, AdjudicationRejected):
        return [], 0
    return findings_from_adjudication_run(run, audit_run_id), run.citations_rejected_nonexistent


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def ablate_run(
    generated: GeneratedRun,
    report,
    ledger: Ledger,
    contract: CompiledContract,
    provider: LLMProvider,
) -> dict[str, tuple[DetectionResult, dict[str, int]]]:
    """Every ablation for one already-audited run, keyed by name."""
    truth = generated.ground_truth
    out: dict[str, tuple[DetectionResult, dict[str, int]]] = {}

    out["no_adjudicator"] = (
        evaluate(without_adjudicator(report.findings), truth),
        {"findings_removed": len(report.findings) - len(without_adjudicator(report.findings))},
    )

    fixed = with_fixed_threshold(report.findings)
    out["no_conformal_calibration"] = (
        evaluate(fixed, truth),
        {
            "would_auto_post": sum(1 for f in fixed if f.lane is Lane.AUTO),
            "would_auto_post_paise": sum(abs(f.amount_impact.paise) for f in fixed if f.lane is Lane.AUTO),
            "actually_auto_posted": sum(1 for f in report.findings if f.lane is Lane.AUTO),
        },
    )

    structural = structural_only_findings(report.proofs, ledger, contract, report.audit_run_id)
    out["structural_only"] = (
        evaluate(structural, truth),
        {
            "credits_resolved": len(structural_only(report.proofs)),
            "credits_total": len(report.proofs),
            "unexplained_paise": structural_only_unexplained_paise(report.proofs, ledger),
        },
    )

    unchecked, fabricated = unchecked_adjudication_findings(
        report.proofs, report.conservation, ledger, report.unclaimed, provider, report.audit_run_id
    )
    out["no_reference_checker"] = (
        evaluate(without_adjudicator(report.findings) + unchecked, truth),
        {"fabricated_ids_admitted": fabricated},
    )
    return out


def build_report(
    baseline_results: Sequence[DetectionResult],
    per_ablation: Sequence[dict[str, tuple[DetectionResult, dict[str, int]]]],
    baseline_notes: dict[str, int] | None = None,
) -> AblationReport:
    """Pool every run's ablations into one table."""
    catalogue = {
        "no_adjudicator": (
            "deterministic engine only",
            "residual hypotheses are never proposed; only core/verify.py's findings remain",
        ),
        "no_conformal_calibration": (
            f"fixed {FIXED_AUTO_THRESHOLD_BPS / 10_000:.1f} confidence threshold",
            "lanes assigned by a round number instead of a fitted, per-source conformal bound",
        ),
        "structural_only": (
            "tier 1 matching only",
            "UTR/batch joins alone -- no subset-sum search, no batched assignment",
        ),
        "no_reference_checker": (
            "model citations trusted",
            "invariant 6's reference check disabled; cited record ids are not verified to exist",
        ),
    }
    rows: list[AblationRow] = []
    for name, (label, description) in catalogue.items():
        results = [run[name][0] for run in per_ablation if name in run]
        if not results:
            continue
        notes: dict[str, int] = {}
        for run in per_ablation:
            for key, value in run.get(name, (None, {}))[1].items():
                notes[key] = notes.get(key, 0) + value
        rows.append(
            AblationRow(
                name=name, label=label, description=description, detection=merge(results), notes=notes
            )
        )
    return AblationReport(
        baseline=merge(baseline_results), baseline_notes=baseline_notes or {}, rows=rows
    )
