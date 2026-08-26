"""Renders EVIDENCE.md from a SweepReport.

EVIDENCE.md is generated and never hand-edited. That is enforced, not
asked for: the file opens with a banner carrying the SHA-256 of everything
below it, and `scripts/check_evidence.py` recomputes that hash and fails if
a single character has moved. A generated accuracy report that someone can
quietly touch up is not evidence of anything.

The section order is fixed and the numbers are printed as they came back.
Where a figure rests on an assumption this project cannot measure -- a
paid-tier token price, a human reconciliation rate -- the assumption is
printed beside it. Where a figure is bad, it is printed anyway: §4 (false
positives) sits near the top on purpose, and §14 says plainly what
synthetic data does and does not prove.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.money import Money
from eval.ablate import FIXED_AUTO_THRESHOLD_BPS
from eval.detect import DATA_QUALITY_FLAG, NO_DISCREPANCY, NOT_DETECTED
from eval.diagram import SVG_PATH
from eval.sweep import (
    ASSUMED_MANUAL_TXNS_PER_HOUR,
    ASSUMED_USD_PER_M_INPUT_TOKENS,
    ASSUMED_USD_PER_M_OUTPUT_TOKENS,
    SweepReport,
)

EVIDENCE_PATH = Path("EVIDENCE.md")
BANNER_END = "-->"

_BANNER = """<!-- GENERATED FILE - DO NOT EDIT BY HAND.
     Regenerate with `make evidence`. Every number below comes from a real
     run of the engine over synthetic data with known planted truth.
     Any manual edit changes the body hash and fails scripts/check_evidence.py.
     body-sha256: {body_sha256} -->"""


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def wrap(body: str) -> str:
    return _BANNER.format(body_sha256=body_hash(body)) + "\n" + body


def split(document: str) -> tuple[str, str]:
    """(declared hash, body). Raises ValueError if the banner is missing or
    malformed -- a file with no banner is not a generated file, and saying
    so is more useful than silently passing it."""
    marker = document.find(BANNER_END)
    if not document.startswith("<!-- GENERATED FILE") or marker == -1:
        raise ValueError("EVIDENCE.md has no generated-file banner")
    header = document[:marker]
    body = document[marker + len(BANNER_END) :]
    body = body.removeprefix("\n")
    for line in header.splitlines():
        if "body-sha256:" in line:
            return line.split("body-sha256:")[1].strip(), body
    raise ValueError("EVIDENCE.md banner carries no body-sha256")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _rupees(paise: int) -> str:
    """`Money.to_rupees_str()` with a symbol and thousands separators.

    The digits come from `Money`, never from arithmetic here -- this adds
    grouping and a currency symbol to an already-formatted string, which is
    presentation, not money handling. A document about rupees should say
    which currency it means, and `61264.62` in running prose does not.
    """
    formatted = Money(paise).to_rupees_str()
    sign = "-" if formatted.startswith("-") else ""
    whole, _, frac = formatted.lstrip("-").partition(".")
    return f"{sign}₹{int(whole):,}.{frac}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _pct2(value: float) -> str:
    return f"{value * 100:.2f}%"


def _path(value: str) -> str:
    """Forward slashes, whatever platform produced the string.

    A path is normalised at the point it is written into the report too;
    this is the second line of defence, so re-rendering an older
    `eval/results/sweep.json` recorded on Windows still produces a document
    that does not differ by the machine that generated it.
    """
    return value.replace("\\", "/")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


# Why a given planted code has no detector. Only codes that actually score
# zero are ever printed, so an entry here is a standing explanation rather
# than a claim about any particular run; a code with no entry falls back to
# the generic reason.
_MISSING_DETECTOR_REASON = {
    "D04": (
        "`core/verify.py::_refund_findings` catches this only when the residual isolates to "
        "exactly one refund's own amount; a credit where this shares a residual with another "
        "discrepancy, or where two refunds of the same amount both match, is left unexplained "
        "rather than guessed"
    ),
    "D06": (
        "a refund attributed to the wrong settlement batch leaves NO residual at all -- both "
        "the true and the wrong batch fully verify against their own corrupted record sets -- "
        "and refunds can legitimately settle in a later cycle than their payment, so "
        "\"settlement must match\" is not a safe rule; no deterministic check is attempted"
    ),
    "D07": "sub-paisa rounding drift surfaces as a tax delta and is reported as `tax_miscalculation`",
    "D09": "a data-quality flag with zero rupee impact, not a discrepancy -- nothing claims it",
}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _section_1(report: SweepReport) -> str:
    detection = report.detection
    hashes = "\n".join(
        f"| `{name}` | `{digest}` |" for name, digest in sorted(report.headline_input_hashes.items())
    )
    return f"""# EVIDENCE

How well Assay actually works, measured against synthetic settlement months
with known planted discrepancies. Generated by `make evidence`.

**The number worth reading first.** Across {len(report.runs)} audited months, the engine
claimed **{_rupees(detection.false_positive_paise)}** that was not there
({detection.false_positive_findings} false-positive findings out of {detection.findings_total}),
while recovering **{_pct(detection.value_recall)}** of the rupees actually planted.
A system that over-claims is worse than one that under-claims, so that number
is stated before any of the flattering ones. §4 breaks it down.

## 1. Reproduction

| | |
|---|---|
| Run timestamp | {report.generated_at} |
| Git commit | `{report.git_commit}` |
| Contract version | `{report.contract_version}` |
| Calibration artifact | `{report.calibration_sha256 or "none loaded"}` |
| Profiles | {", ".join(report.profiles)} |
| Seeds evaluated | {", ".join(str(s) for s in report.seeds)} |
| Seeds the calibration was fitted on | {", ".join(str(s) for s in report.held_out_from_seeds)} |
| Python | {report.python_version} |
| Platform | {report.platform} |
| Wall clock (whole sweep) | {report.wall_clock_seconds:.1f}s |

The evaluation seeds and the calibration seeds are disjoint. §8 and §9
would otherwise be reporting how well the calibration artifact memorised
its own training data.

**Model at each LLM boundary**

| Boundary | Model |
|---|---|
| `llm/contract_parser.py` | `{report.models.contract_parser}` |
| `llm/adjudicator.py` | `{report.models.adjudicator}` |
| `llm/narrator.py` | `{report.models.narrator}` |

**Input file hashes** — SHA-256 of the four files of the committed
reference run, `{_path(report.headline_run_dir)}`, which §12's determinism check
audits. The {len(report.runs)} sweep runs are generated in memory from
(profile, seed); their per-run input hashes are in `eval/results/`.

| File | SHA-256 |
|---|---|
{hashes or "| _(reference run not present)_ | |"}
"""


def _section_2(report: SweepReport) -> str:
    detection = report.detection
    code_rows = [
        [
            row.code,
            ", ".join(row.classes),
            str(row.support),
            str(row.true_positives),
            str(row.false_negatives),
            _pct(row.recall),
            _pct(row.value_recall),
        ]
        for row in detection.code_rows
    ]
    total_support = sum(r.support for r in detection.code_rows)
    total_tp = sum(r.true_positives for r in detection.code_rows)
    code_rows.append(
        [
            "**all**",
            "",
            f"**{total_support}**",
            f"**{total_tp}**",
            f"**{total_support - total_tp}**",
            f"**{_pct(detection.count_recall)}**",
            f"**{_pct(detection.value_recall)}**",
        ]
    )

    class_rows = [
        [
            row.discrepancy_class,
            str(row.support),
            str(row.true_positives),
            str(row.false_positives),
            str(row.false_negatives),
            _pct(row.precision),
            _pct(row.recall),
            _pct(row.f1),
        ]
        for row in detection.class_rows
        if row.support or row.true_positives or row.false_positives
    ]
    class_rows.append(
        [
            "**macro avg**", "", "", "", "",
            f"**{_pct(detection.macro.precision)}**",
            f"**{_pct(detection.macro.recall)}**",
            f"**{_pct(detection.macro.f1)}**",
        ]
    )
    class_rows.append(
        [
            "**micro avg**", "", "", "", "",
            f"**{_pct(detection.micro.precision)}**",
            f"**{_pct(detection.micro.recall)}**",
            f"**{_pct(detection.micro.f1)}**",
        ]
    )

    return f"""
## 2. Detection quality

Two tables, because one would have to invent a number.

`DiscrepancyEntry.code` (D01–D12) is finer than `DiscrepancyClass`: five
codes (D01, D03, D10, D11, D12) all plant a fee over/undercharge, and D04
and D06 both plant a refund mismatch. A **true positive is attributable to
a code**, through the records it touched. A **false positive is not** —
there is no fact of the matter about which code a spurious fee finding
"should" have been. So recall lives in table A, where it is exact, and
precision lives in table B, where it is.

**Table A — per planted code**

{_table(["code", "class(es)", "support", "TP", "FN", "recall", "value-recall"], code_rows)}

D09 is a data-quality flag, not a discrepancy: it corrupts a UTR and costs
zero rupees, and no detector in `core/verify.py` claims it. Its row reads
what it is rather than being excluded.

**Table B — per discrepancy class**

{_table(["class", "support", "TP", "FP", "FN", "precision", "recall", "F1"], class_rows)}

Macro average is over classes actually planted or predicted; including
never-exercised taxonomy members would report how many enum members exist
rather than how well the engine did.

> **The FP column here is not §4's number, and the two must not be
> conflated.** This table derives from §5's confusion matrix in the
> textbook way: a class's false positives are every finding that *predicted*
> that class whose true class was something else — which includes findings
> that landed on a real planted defect and named the wrong class. §4's
> headline counts only findings that touch no planted record at all. Both
> definitions are standard; they answer different questions. "How often is
> this class's label right?" is this table. "How many rupees did the system
> claim that were never missing?" is §4, and it is the smaller and more
> serious number.

**A third outcome, counted separately.** {detection.misclassified_findings}
findings ({_rupees(detection.misclassified_paise)}) landed on a genuinely
planted defect but named a different class. Those are neither successes nor
false claims: one planted D01 changes a fee line *and* the tax computed on
it, so the engine correctly emits a fee finding and a tax finding, and only
the first matches D01's own class. They are counted as misclassifications,
kept out of §4's "falsely claimed" total, and visible in §5's matrix.
"""


def _section_3(report: SweepReport) -> str:
    rows = [
        [
            row.code,
            _rupees(row.planted_paise),
            _rupees(row.detected_paise),
            _pct(row.value_recall),
            _pct(row.recall),
        ]
        for row in report.detection.code_rows
    ]
    rows.append(
        [
            "**all**",
            f"**{_rupees(report.detection.planted_paise)}**",
            f"**{_rupees(report.detection.detected_paise)}**",
            f"**{_pct(report.detection.value_recall)}**",
            f"**{_pct(report.detection.count_recall)}**",
        ]
    )
    gap = report.detection.count_recall - report.detection.value_recall
    direction = "below" if gap > 0 else "above"
    return f"""
## 3. Value-weighted recall

Rupees of planted loss detected, over rupees planted. This matters more
than count-recall: missing one ₹40,000 error is worse than missing forty
₹100 errors. Both are shown so the difference is visible.

{_table(["code", "planted", "detected", "value-recall", "count-recall"], rows)}

Overall value-recall is **{_pct(report.detection.value_recall)}** against a
count-recall of **{_pct(report.detection.count_recall)}** — value-recall sits
{abs(gap) * 100:.1f} percentage points {direction} count-recall, meaning the
errors this engine misses are {"larger" if gap > 0 else "smaller"} than average.
That gap is the point of this section: a system judged only on how many
discrepancies it caught would look {"far better" if gap > 0 else "worse"} than one
judged on how many rupees it recovered.
"""


def _section_4(report: SweepReport) -> str:
    detection = report.detection
    rows = []
    for profile, result in report.detection_by_profile.items():
        rows.append(
            [
                profile,
                str(result.findings_total),
                str(result.false_positive_findings),
                _rupees(result.false_positive_paise),
                _pct2(result.false_positive_findings / result.findings_total if result.findings_total else 0.0),
            ]
        )
    rows.append(
        [
            "**all**",
            f"**{detection.findings_total}**",
            f"**{detection.false_positive_findings}**",
            f"**{_rupees(detection.false_positive_paise)}**",
            f"**{_pct2(detection.false_positive_findings / detection.findings_total if detection.findings_total else 0.0)}**",
        ]
    )
    class_rows = [
        [
            discrepancy_class,
            str(count),
            _rupees(detection.false_positive_paise_by_class.get(discrepancy_class, 0)),
        ]
        for discrepancy_class, count in sorted(
            detection.false_positive_by_class.items(), key=lambda kv: -kv[1]
        )
    ]

    from_adjudicator = detection.false_positive_from_adjudicator
    share = from_adjudicator / detection.false_positive_findings if detection.false_positive_findings else 0.0
    paise_share = (
        detection.false_positive_paise_from_adjudicator / detection.false_positive_paise
        if detection.false_positive_paise
        else 0.0
    )
    attribution = (
        f"**{from_adjudicator} of {detection.false_positive_findings} false positives "
        f"({_pct(share)}), and {_rupees(detection.false_positive_paise_from_adjudicator)} of the "
        f"{_rupees(detection.false_positive_paise)} ({_pct(paise_share)}), came from the LLM "
        "adjudicator** — not from the deterministic engine. §13's first ablation prices exactly "
        "what removing that boundary would buy and cost."
        if from_adjudicator
        else "**None of these came from the LLM adjudicator**; every one was produced by the "
        "deterministic engine in `core/verify.py`."
    )

    return f"""
## 4. False-positive money impact

**{detection.false_positive_findings} false-positive findings, claiming
{_rupees(detection.false_positive_paise)} that was not there.**

A false positive here is a finding whose evidence touches *no* planted
record at all — it points somewhere nothing was wrong. A finding that lands
on a real defect but names the wrong class is a misclassification
({detection.misclassified_findings} of them,
{_rupees(detection.misclassified_paise)}) and is excluded from this total,
because it did not claim money that was not there.

{_table(["profile", "findings", "false positives", "rupees falsely claimed", "FP rate"], rows)}

**Which boundary produced them.** {attribution}

**What they claimed to be**

{_table(["claimed class", "count", "rupees"], class_rows) if class_rows else "_No false positives._"}

A merchant taking these findings to a gateway is putting their credibility
behind each one. Over-claiming costs more than under-claiming, which is why
this section is the fourth thing in the document and not the last.
"""


def _section_5(report: SweepReport) -> str:
    confusion = report.detection.confusion
    predicted_labels = sorted({p for row in confusion.values() for p in row if p != NOT_DETECTED})
    headers = ["true \\ predicted", *predicted_labels, NOT_DETECTED]
    rows = []
    for true_class in sorted(confusion):
        counts = confusion[true_class]
        label = true_class
        if true_class == NO_DISCREPANCY:
            label = f"**{NO_DISCREPANCY}**"
        elif true_class == DATA_QUALITY_FLAG:
            label = f"{DATA_QUALITY_FLAG} (D09)"
        cells = [str(counts.get(p, 0)) for p in predicted_labels]
        cells.append(str(counts.get(NOT_DETECTED, 0)))
        rows.append([label, *cells])
    return f"""
## 5. Confusion matrix

Rows are what was planted; columns are what the engine called it. The
`{NOT_DETECTED}` column is a plant nothing found. The `{NO_DISCREPANCY}` row
is a finding on records where nothing was planted — that row is §4's
false-positive count, laid out by the class each one claimed.

{_table(headers, rows)}
"""


def _section_6(report: SweepReport) -> str:
    by_tier: dict[str, int] = {}
    resolved_by_tier: dict[str, int] = {}
    reasons: dict[str, int] = {}
    credits = ambiguous = unresolved = 0
    for run in report.runs:
        credits += run.decomposition.credits
        ambiguous += run.decomposition.ambiguous
        unresolved += run.decomposition.unresolved
        for tier, count in run.decomposition.by_tier.items():
            by_tier[tier] = by_tier.get(tier, 0) + count
        for tier, count in run.decomposition.resolved_by_tier.items():
            resolved_by_tier[tier] = resolved_by_tier.get(tier, 0) + count
        for reason, count in run.decomposition.unresolved_reasons.items():
            reasons[reason] = reasons.get(reason, 0) + count

    tier_rows = [
        [
            tier,
            str(by_tier.get(tier, 0)),
            str(resolved_by_tier.get(tier, 0)),
            _pct(resolved_by_tier.get(tier, 0) / credits if credits else 0.0),
        ]
        for tier in ("structural", "subset_sum", "assignment")
    ]
    total_resolved = sum(resolved_by_tier.values())
    tier_rows.append(
        ["**all**", f"**{credits}**", f"**{total_resolved}**", f"**{_pct(total_resolved / credits if credits else 0.0)}**"]
    )

    reason_table = (
        _table(["reason", "credits"], [[reason, str(count)] for reason, count in sorted(reasons.items())])
        if reasons
        else "_No credit failed to resolve in this sweep._"
    )

    return f"""
## 6. Decomposition quality

Every bank credit is decomposed into the exact transactions that produced
it, through three tiers: structural (UTR/batch join), subset-sum search,
and a batched assignment.

{_table(["tier", "credits reaching it", "resolved there", "share of all credits"], tier_rows)}

**Ambiguous but reported: {ambiguous}.** {
    "Every one of these is a SUCCESS, not a failure."
    if ambiguous
    else "None arose in this sweep -- but the policy is what matters, and it is worth stating."
} An ambiguous outcome means two or more transaction sets explain the same
credit equally well, and the engine says so and names the competing
candidates rather than picking one. Reporting the ambiguity is the correct
answer; silently choosing would produce a confident, fully-verifying proof
over possibly the wrong records — the exact failure `core/decompose.py`
exists to make impossible.{
    "" if ambiguous else " A zero here reflects this generator's own data, "
    "which gives most credits a clean structural key; it is not evidence that "
    "real settlement files are unambiguous."
}

**Unresolved: {unresolved}**, broken down by the reason each one names.
The vocabulary is fixed (`DecompositionReason`); no free-text reasons exist
to be uncountable here.

{reason_table}
"""


def _section_7(report: SweepReport) -> str:
    unexplained = sum(r.conservation.total_unexplained_paise for r in report.runs)
    unclaimed = sum(r.conservation.unclaimed_paise for r in report.runs)
    unaccounted = sum(r.conservation.total_unaccounted_paise for r in report.runs)
    volume = sum(r.conservation.settled_gross_paise for r in report.runs)
    bps = abs(unaccounted) * 10_000 / volume if volume else 0.0

    profile_rows = []
    for profile in report.profiles:
        runs = [r for r in report.runs if r.profile == profile]
        if not runs:
            continue
        profile_unaccounted = sum(r.conservation.total_unaccounted_paise for r in runs)
        profile_volume = sum(r.conservation.settled_gross_paise for r in runs)
        profile_rows.append(
            [
                profile,
                _rupees(sum(r.conservation.total_unexplained_paise for r in runs)),
                _rupees(sum(r.conservation.unclaimed_paise for r in runs)),
                _rupees(profile_unaccounted),
                f"{abs(profile_unaccounted) * 10_000 / profile_volume if profile_volume else 0.0:.2f}",
            ]
        )

    clean_ok = report.clean_profile_unaccounted_paise == 0
    clean_line = (
        "**The clean profile reconciles to exactly zero.** "
        f"`{_rupees(report.clean_profile_unaccounted_paise)}` unaccounted across every clean-profile "
        "run — not rounded to zero, not within tolerance. Zero."
        if clean_ok
        else "**The clean profile did NOT reconcile to zero.** "
        f"`{_rupees(report.clean_profile_unaccounted_paise)}` is unaccounted, which is a defect: a "
        "month with nothing planted must reconcile exactly. This is reported rather than suppressed."
    )

    return f"""
## 7. Conservation

For every bank credit, in paise, with no tolerance:

```
credit = settled_gross - refunds - fees - tax - chargebacks - adjustments
       + reversals + unexplained
```

`unexplained` is a first-class reportable bucket, asserted at the end of
every run. It has two halves, and reporting only the first would let a
whole lost settlement batch read as clean: residual on credits that
exist, plus records no credit ever claimed.

| | |
|---|---|
| Unexplained (residual on credits) | {_rupees(unexplained)} |
| Unclaimed (records no credit claimed) | {_rupees(unclaimed)} |
| **Total unaccounted** | **{_rupees(unaccounted)}** |
| Settled volume | {_rupees(volume)} |
| Unaccounted as bps of volume | **{bps:.2f} bps** |

{_table(["profile", "unexplained", "unclaimed", "total unaccounted", "bps of volume"], profile_rows)}

{clean_line}
"""


def _section_8(report: SweepReport) -> str:
    calibration = report.calibration
    rows = [
        [
            f"{b.bin_lo_bps / 10_000:.1f}–{b.bin_hi_bps / 10_000:.1f}",
            str(b.count),
            f"{b.mean_confidence:.3f}",
            f"{b.empirical_accuracy:.3f}",
            f"{b.empirical_accuracy - b.mean_confidence:+.3f}",
        ]
        for b in calibration.bins
        if b.count
    ]
    return f"""
## 8. Calibration

Predicted confidence against observed accuracy, over
{calibration.total:,} labelled decisions from the three raw confidence
sources (decomposition tier, deterministic recomputation, adjudicator
coverage).

{_table(["confidence bucket", "n", "mean predicted", "observed accuracy", "gap"], rows)}

| | |
|---|---|
| Expected calibration error | **{calibration.expected_calibration_error:.4f}** |
| Brier score | **{calibration.brier_score:.4f}** |

A positive gap is underconfidence — the engine was right more often than it
claimed. A negative gap is the dangerous direction.

![Reliability diagram]({SVG_PATH.as_posix()})

The diagram is committed at `{SVG_PATH.as_posix()}` (byte-deterministic, so a
regeneration over unchanged results produces an unchanged file) and at
`docs/reliability.png`.
"""


def _section_9(report: SweepReport) -> str:
    conformal = report.conformal
    total_findings = sum(conformal.lane_counts.values())
    total_paise = sum(conformal.lane_paise.values())
    lane_rows = [
        [
            lane,
            str(count),
            _pct(count / total_findings if total_findings else 0.0),
            _rupees(conformal.lane_paise.get(lane, 0)),
            _pct(conformal.lane_paise.get(lane, 0) / total_paise if total_paise else 0.0),
        ]
        for lane, count in sorted(conformal.lane_counts.items())
    ]

    if conformal.auto_reachable:
        coverage_rows = [
            [source, str(counts[0]), str(counts[1]), _pct2(counts[1] / counts[0] if counts[0] else 0.0)]
            for source, counts in sorted(conformal.coverage_by_source.items())
        ]
        coverage = _table(["source", "n at/above threshold", "correct", "empirical accuracy"], coverage_rows)
        threshold_line = f"`{conformal.auto_min_calibrated_bps}` bps calibrated confidence"
        guarantee = (
            "Empirical coverage on the held-out split is above. A source whose own "
            "Clopper-Pearson bound could not certify the target never contributes to AUTO: "
            "the bounds are computed per source and never pooled, so a high-volume accurate "
            "source cannot statistically hide a low-volume inaccurate one."
        )
    else:
        coverage = (
            "_No coverage table: the fit could not certify the target error rate at any "
            "threshold, for at least one confidence source, so AUTO is unreachable and the "
            "set of auto-posted items is empty._"
        )
        threshold_line = "**unreachable** — no threshold was certified"
        guarantee = (
            "This is the honest outcome, not a missing feature. `auto_min_calibrated_bps` is "
            "`null` in the committed artifact, and `core/lanes.py` keeps it `null` rather than "
            "clamping it to 10,000 — which would turn \"no threshold satisfies the guarantee\" "
            "into \"the threshold is exactly certainty\", a strictly more permissive claim than "
            "the fit supports. Nothing auto-posts. The consequence is visible in §13's "
            "calibration ablation: a fixed 0.9 threshold *would* have auto-posted, and what it "
            "would have posted is exactly the risk the conformal fit refused to certify."
        )

    return f"""
## 9. Conformal guarantee

| | |
|---|---|
| Target error rate | {conformal.target_error_bps} bps ({conformal.target_error_bps / 100:.2f}%) |
| Confidence level | {conformal.confidence_level_bps / 100:.0f}% |
| Derived AUTO threshold | {threshold_line} |
| ESCALATE threshold | ≤ {conformal.escalate_max_calibrated_bps} bps |
| Fitted on seeds | {", ".join(str(s) for s in conformal.fitted_on_seeds) or "n/a"} |
| Evaluated on seeds | {", ".join(str(s) for s in conformal.evaluated_on_seeds)} |

{coverage}

{guarantee}

**Volume share by lane**

{_table(["lane", "findings", "share of findings", "rupees", "share of rupees"], lane_rows)}
"""


def _section_10(report: SweepReport) -> str:
    llm = report.llm
    telemetry = llm.telemetry
    estimated_note = (
        f" ({telemetry.estimated_token_calls} of {telemetry.calls} calls' tokens are byte-length "
        "estimates, from cache entries recorded before usage tracking existed; the rest are the "
        "API's own reported counts)"
        if telemetry.estimated_token_calls
        else " (all measured from the API's own reported usage)"
    )
    # Omitted rather than printed as zeros when the sweep predates this
    # field: "0 RPM, 0 retries" would be a false statement, and an absent
    # row is the honest rendering of an unrecorded measurement.
    limits = report.limits
    limits_row = (
        f"\n| Rate-limit config this ran under | {limits.rpm} RPM, {limits.max_retries} retries, "
        f"{limits.backoff_base_seconds:.1f}s base backoff |"
        if limits.rpm
        else ""
    )
    unattributed = telemetry.total_tokens - telemetry.prompt_tokens - telemetry.response_tokens
    reasoning_note = (
        ""
        if unattributed <= 0
        else (
            f"\n**Prompt and response do not sum to the total**, and that is the API's own "
            f"accounting rather than an error here: {unattributed:,} tokens are billed to the "
            "call but attributed to neither side — reasoning tokens the model spent before "
            "answering. The total is what a bill would show, so the total is what the cost "
            "below is computed from.\n"
        )
    )
    return f"""
## 10. LLM boundary

This is the AI-judgment argument, in numbers. The engine is deterministic
Python; a model is consulted at exactly three boundaries and never computes,
adjusts or approves an amount.

| | |
|---|---|
| **Records that touched a model at all** | **{llm.records_touching_a_model:,} of {llm.records_total:,} ({_pct2(llm.records_touched_fraction)})** |
| Total provider calls (whole sweep) | {telemetry.calls} |
| Calls per 1,000 records | {llm.calls_per_1000_records:.2f} |
| Residuals submitted for adjudication | {llm.residuals_submitted} |
| Schema validation rejection rate | {_pct2(llm.schema_rejection_rate)} ({llm.schema_rejections} of {llm.schema_rejection_opportunities}) |
| Reference-check rejection rate | {_pct2(llm.reference_rejection_rate)} ({llm.citations_rejected_nonexistent + llm.citations_rejected_not_in_pool} of {llm.citations_total} citations) |
| **Fabricated record IDs caught** | **{llm.citations_rejected_nonexistent}** |
| Real records cited but never shown | {llm.citations_rejected_not_in_pool} |
| Cache hit rate | {_pct(telemetry.cache_hit_rate_bps / 10_000)} ({telemetry.cache_hits} hits / {telemetry.calls} calls) |
| Calls that reached the network | {telemetry.live_calls} |
| 429s observed | {telemetry.rate_limited} |
| Time spent in backoff | {telemetry.backoff_seconds:.1f}s |{limits_row}
| Tokens (prompt / response / total) | {telemetry.prompt_tokens:,} / {telemetry.response_tokens:,} / {telemetry.total_tokens:,} |
| Tokens per 1,000 records | {llm.tokens_per_1000_records:,.0f} |
| Runs where adjudication degraded | {llm.degraded_runs} of {len(report.runs)} |

**Token counts**{estimated_note}.
{reasoning_note}
**What this would cost.** The build ran entirely on the Gemini free tier and
paid nothing. At an assumed paid-tier list price of
${ASSUMED_USD_PER_M_INPUT_TOKENS:.2f}/M input and
${ASSUMED_USD_PER_M_OUTPUT_TOKENS:.2f}/M output tokens, this sweep's
{telemetry.total_tokens:,} tokens would cost **${llm.assumed_cost_usd:.4f}** —
about **${llm.assumed_cost_usd / llm.records_total * 1000:.4f} per 1,000
records** audited. Those rates are an assumption, not a measurement; vendor
pricing moves, and the figure to update is in `eval/sweep.py`.

A schema-invalid batch response aborts its whole batch rather than being
repaired, and an unavailable provider degrades the adjudication rather than
guessing — so a rejection rate of zero above means no rejection occurred,
never that one was absorbed.
"""


def _section_11(report: SweepReport) -> str:
    throughput = report.throughput
    cold = f"{throughput.cold_cache_seconds:.1f}s" if throughput.cold_cache_seconds else "n/a (no cache misses)"
    warm = f"{throughput.warm_cache_seconds:.1f}s" if throughput.warm_cache_seconds else "n/a"
    return f"""
## 11. Throughput

| | |
|---|---|
| Records audited | {throughput.records:,} |
| Payments audited | {throughput.payments:,} |
| Wall clock (audit time only) | {throughput.wall_clock_seconds:.1f}s |
| Records/sec | **{throughput.records_per_second:,.0f}** |
| First run, cold cache | {cold} |
| A run served entirely from cache | {warm} |

**Analyst-hours equivalent.** At an assumed
{ASSUMED_MANUAL_TXNS_PER_HOUR} transactions reconciled per hour by hand —
a deliberately generous rate, since a pessimistic one would flatter the
tool — the {throughput.payments:,} payments here represent about
**{throughput.analyst_hours_equivalent:,.0f} analyst-hours**, done in
{throughput.wall_clock_seconds:.0f} seconds. The assumption is stated so it
can be argued with; the honest comparison is not "faster than a human" but
"a human would not do this at all, at this volume, line by line".
"""


def _section_12(report: SweepReport) -> str:
    determinism = report.determinism
    verdict = "**match**" if determinism.matches else "**DO NOT MATCH — invariant 4 is broken**"
    replay_verdict = "matches" if determinism.replay_matches else "**DOES NOT MATCH**"
    return f"""
## 12. Determinism

Invariant 4 promises that the same inputs, contract version and seed
produce a byte-identical report carrying a SHA-256 of its own canonical
JSON. Checked by auditing `{_path(determinism.run_dir)}` twice and comparing.

| | |
|---|---|
| Run 1 report hash | `{determinism.first_hash}` |
| Run 2 report hash | `{determinism.second_hash}` |
| Verdict | {verdict} |
| Replay hash (cache only, no network) | `{determinism.replay_hash}` |
| Replay {replay_verdict}, live API calls made | {determinism.replay_made_live_calls} |

The hash covers what the audit concluded and excludes what it observed
about the machine it ran on: per-proof `elapsed_ns`, the run's own start
time and wall clock, each journal entry's `posted_at`, and all LLM
telemetry. It does *not* exclude calibrated lane assignments — the
calibration artifact is an input, and `calibration_sha256` records which
one was used.

A run whose decomposition hit the wall-clock budget carries no
byte-identical guarantee at all and is refused rather than scored
(`eval/determinism.py`). No run in this sweep hit it.
"""


def _section_13(report: SweepReport) -> str:
    ablation = report.ablation
    if not ablation.rows:
        return "\n## 13. Ablation study\n\n_Not run for this sweep._\n"

    rows = [
        [
            "**baseline (everything on)**",
            f"**{_pct(ablation.baseline.value_recall)}**",
            "—",
            f"**{_rupees(ablation.baseline.false_positive_paise)}**",
            "—",
        ]
    ]
    unchanged: list[str] = []
    for row in ablation.rows:
        delta_recall = ablation.delta_value_recall(row)
        delta_fp = ablation.delta_false_positive_paise(row)
        if delta_recall == 0 and delta_fp == 0:
            unchanged.append(row.label)
        rows.append(
            [
                row.label,
                _pct(row.value_recall),
                f"{delta_recall * 100:+.1f}pp",
                _rupees(row.false_positive_paise),
                f"{'+' if delta_fp >= 0 else '−'}{_rupees(abs(delta_fp))}",
            ]
        )

    notes = []
    for row in ablation.rows:
        if not row.notes:
            continue
        detail = ", ".join(
            f"{key.replace('_', ' ').removesuffix(' paise')}: "
            f"{_rupees(value) if key.endswith('_paise') else f'{value:,}'}"
            for key, value in sorted(row.notes.items())
        )
        notes.append(f"- **{row.label}** — {row.description}. {detail}.")

    unchanged_note = (
        ""
        if not unchanged
        else (
            "\n**A row with no delta is a real result, not a missing measurement.** "
            + ", ".join(f"*{label}*" for label in unchanged)
            + " changed neither recall nor false-positive rupees in this sweep. For the "
            "reference checker that follows directly from §10: the model fabricated "
            "**zero** record ids here, so a checker that catches fabrications had nothing "
            "to catch. It is insurance that did not fire — which is evidence about this "
            "sweep's model output, not evidence that the check is unnecessary. Removing a "
            "guard is only free until the day it isn't.\n"
        )
    )

    return f"""
## 13. Ablation study

Does the AI actually earn its place. Each component is disabled, one at a
time, and the whole sweep is re-scored.

{_table(["configuration", "value-recall", "Δ", "rupees falsely claimed", "Δ"], rows)}

{chr(10).join(notes)}
{unchanged_note}
Two of these deserve reading together. Removing the **adjudicator** costs
recall on residuals that deterministic matching could not explain — that is
what the model is for. Removing **conformal calibration** costs nothing in
recall and everything in safety: a fixed
{FIXED_AUTO_THRESHOLD_BPS / 10_000:.1f} threshold auto-posts findings the
calibrated system refuses to, and the calibrated system refuses precisely
because the fit could not certify that error rate. The reference-checker
ablation prices invariant 6 directly, in fabricated record ids that would
otherwise have reached a report.

The structural-only row is a genuine re-run, not an approximation:
`decompose_all` completes its structural phase over every credit before any
credit falls to tier 2, so a structural result never depends on what the
later tiers did.
"""


def _section_14(report: SweepReport) -> str:
    # The classes this engine simply does not detect, ranked by the money
    # they carry. Computed, not written down: a limitations section that
    # has to be remembered to be updated is a limitations section that goes
    # stale, and this is the one place staleness would flatter the tool.
    missed = sorted(
        (row for row in report.detection.code_rows if row.true_positives == 0 and row.planted_paise > 0),
        key=lambda row: -row.planted_paise,
    )
    missed_total = sum(row.planted_paise for row in missed)
    missed_share = missed_total / report.detection.planted_paise if report.detection.planted_paise else 0.0
    missed_block = (
        ""
        if not missed
        else (
            "\n**The largest single gap: classes with no detector at all.** "
            f"{_rupees(missed_total)} of the {_rupees(report.detection.planted_paise)} planted "
            f"({_pct(missed_share)}) sits in discrepancy classes this engine never claims — not "
            "misclassified, not low-confidence, simply not looked for. This is most of the "
            "distance between the value-recall above and 100%, and it is a missing feature "
            "rather than a broken one.\n\n"
            + _table(
                ["code", "class(es)", "planted", "why"],
                [
                    [
                        row.code,
                        ", ".join(row.classes),
                        _rupees(row.planted_paise),
                        _MISSING_DETECTOR_REASON.get(
                            row.code, "no rule in `core/verify.py` produces this class"
                        ),
                    ]
                    for row in missed
                ],
            )
            + "\n"
        )
    )

    fabricated = report.llm.citations_rejected_nonexistent
    reference_check_claim = (
        f"validated hard enough that {fabricated} fabricated record ids were caught rather "
        "than published"
        if fabricated
        else "validated by a reference check that had nothing to catch in this sweep — the "
        "model fabricated zero record ids here, which is a fact about this sweep's output "
        "and not a demonstration that the check works"
    )

    return f"""
## 14. Limitations

Plainly, without softening.

**This is synthetic data.** Every number above is measured against months
generated by `datagen/`, where the truth is known because this project
planted it. That proves the engine finds what `datagen/` knows how to hide,
and nothing more. It does not prove the engine finds what a real gateway
does wrong.

**What it does prove.** The arithmetic is right, the conservation identity
holds exactly on data engineered to break it, the decomposition resolves
real many-to-many credit/batch structure, ambiguity is reported rather than
guessed, and the model boundary is small enough to measure
({_pct2(report.llm.records_touched_fraction)} of records) and
{reference_check_claim}.
{missed_block}
**What would change against production data.**
- Real rate cards are worse than generated ones: PDFs, addenda in email,
  clauses that contradict each other. `llm/contract_parser.py` is validated
  against schema, overlap and completeness, but there is no second
  independent check that it *read the card correctly*, and no test yet
  asserts a compiled contract reproduces the undisputed majority of a real
  settlement's own fees.
- The generator gives most credits a distinct UTR pointing at a distinct
  batch. Real settlement files do not guarantee that bijection, and the
  tiers below structural matching would carry far more of the load.
- `core/verify.py` matches a won chargeback to its reversal by amount
  alone, ignoring `Adjustment.reason` and `settlement_id`. Round rupee
  amounts recur in production; this would false-negative.
- Currency is assumed to be INR throughout. `core/contract.py` does not
  check a payment's currency against the rate card's declared currency, so
  a foreign-currency payment would be priced by whichever INR-paise band
  its minor-unit amount happened to land in.

**Known gaps in what is measured here.**
- D09 (corrupted UTR) has no detector. It is a data-quality flag with zero
  rupee impact, and its recall is honestly 0.
- The clean profile plants nothing, so it contributes support to no class.
  It is included because a zero-discrepancy month reconciling to exactly
  zero is itself a result worth showing (§7).
- The contract used by every run above has no recorded human sign-off.
  `core/contract.py::require_signoff` exists and is tested, but no
  `*.signoff.json` is committed and `run_audit` does not gate on one. A
  production deployment must.
- {len(report.runs)} runs on {len(report.seeds)} seeds per profile is enough
  to see the shape of the per-class numbers and not enough to put a tight
  confidence interval on the sparse classes — D03 and D05 plant as few as
  one instance per month.
"""


def render(report: SweepReport) -> str:
    body = "".join(
        section(report)
        for section in (
            _section_1, _section_2, _section_3, _section_4, _section_5, _section_6, _section_7,
            _section_8, _section_9, _section_10, _section_11, _section_12, _section_13, _section_14,
        )
    )
    return wrap(body)


def write(report: SweepReport, path: Path = EVIDENCE_PATH) -> Path:
    path = Path(path)
    path.write_text(render(report), encoding="utf-8")
    return path
