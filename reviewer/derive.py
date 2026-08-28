"""Every number the reviewer UI shows, derived from a persisted AuditReport.

Pure functions over an `AuditReport`, kept out of `api.py` so they can be
tested without a server. No money arithmetic happens beyond summing and
subtracting integer paise -- floats never touch a rupee figure here, and
each response carries both the exact `_paise` integer and a preformatted
rupee string so the browser never has to do money math in IEEE 754 either.
"""

from __future__ import annotations

from pydantic import BaseModel

from cli.audit import AuditReport
from core.decompose import DecompositionOutcome
from core.models import Lane
from core.money import Money


def rupees(paise: int) -> str:
    return Money(paise).to_rupees_str()


def _group_indian(digits: str) -> str:
    """Indian digit grouping (last 3, then pairs): 6490836 -> 64,90,836.

    String surgery on the digits Money already produced, never a parse to
    float and back -- the grouped form must be the same number, just easier
    to read.
    """
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def rupees_display(paise: int) -> str:
    """`₹64,90,836.77` -- grouped, symbol-prefixed, for prose and labels."""
    text = rupees(paise)
    negative = text.startswith("-")
    body = text[1:] if negative else text
    whole, _, frac = body.partition(".")
    return f"{'-' if negative else ''}₹{_group_indian(whole)}.{frac}"


class MoneyView(BaseModel):
    """One amount, twice: the exact integer the engine computed, and the
    string the browser should print. The browser never divides by 100."""

    paise: int
    rupees: str

    @classmethod
    def of(cls, paise: int) -> MoneyView:
        return cls(paise=paise, rupees=rupees(paise))


def settled_gross_paise(report: AuditReport) -> int:
    """Total gross settled across every bank credit this run decomposed.

    `AuditReport` carries no such field -- it records what could NOT be
    accounted for, not the volume it was measured against. The same sum is
    derived in `eval/harness.py::_settled_gross_paise` and again in
    `eval/evidence.py`; it is re-derived here rather than imported because
    `eval/` reads `datagen/` ground truth and importing it would put a path
    from a user-facing server to the answers (invariant 5).
    """
    return sum(c.settled_gross_paise for c in report.conservation)


class LaneBucket(BaseModel):
    lane: str
    finding_count: int
    impact: MoneyView
    posts_automatically: bool
    description: str


_LANE_COPY = {
    Lane.AUTO: (
        True,
        (
            "Calibrated above the certification threshold. These post to the append-only "
            "journal without a human in the loop."
        ),
    ),
    Lane.PROPOSE: (
        False,
        (
            "The engine has a position but not enough calibrated confidence to post it. "
            "A reviewer decides. Nothing here has touched the ledger."
        ),
    ),
    Lane.ESCALATE: (
        False,
        (
            "Below the confidence floor, or sourced from a model. Routed to a human by "
            "construction -- core/lanes.py hard-caps LLM-sourced items out of AUTO."
        ),
    ),
}


def lane_buckets(report: AuditReport) -> list[LaneBucket]:
    """The three lanes with counts and rupee impact, always all three and
    always in AUTO -> PROPOSE -> ESCALATE order: a lane with zero findings
    is itself a fact worth showing, so empty buckets are never dropped."""
    buckets = []
    for lane in (Lane.AUTO, Lane.PROPOSE, Lane.ESCALATE):
        findings = [f for f in report.findings if f.lane is lane]
        posts, description = _LANE_COPY[lane]
        buckets.append(
            LaneBucket(
                lane=lane.value,
                finding_count=len(findings),
                impact=MoneyView.of(sum(f.amount_impact.paise for f in findings)),
                posts_automatically=posts,
                description=description,
            )
        )
    return buckets


class BatchView(BaseModel):
    run_id: str
    run_dir: str
    merchant_id: str
    contract_version: str
    seed: int
    report_hash: str
    git_sha: str
    rounding_policy: str
    input_hash: str

    settled_gross: MoneyView
    verified: MoneyView
    unexplained: MoneyView
    unclaimed: MoneyView
    unaccounted: MoneyView
    unaccounted_bps_of_volume: int

    record_count: int
    credit_count: int
    credits_resolved: int
    finding_count: int
    cluster_count: int
    journal_entry_count: int
    contract_gap_count: int

    adjudication_degraded: bool
    adjudication_degraded_reason: str | None
    lanes: list[LaneBucket]


def _bps_of_volume(unaccounted_paise: int, volume_paise: int) -> int:
    """Unaccounted money as basis points of settled volume, in integer bps.

    Integer arithmetic on purpose: this figure sits next to rupee amounts on
    the same screen and must not be the one number on it that came from a
    float. Zero volume yields 0 rather than dividing.
    """
    if volume_paise == 0:
        return 0
    return abs(unaccounted_paise) * 10_000 // volume_paise


def batch_view(report: AuditReport) -> BatchView:
    """The headline screen: what was settled, what reconciled, what didn't.

    `verified` is defined as settled gross minus the ABSOLUTE unaccounted
    total. The absolute value is deliberate and worth stating plainly: a
    negative residual (the engine explaining more than the credit paid) is
    exactly as unverified as a positive one, and netting the two directions
    against each other would let an overcharge cancel a shortfall and
    report the pair as clean. Both components are returned alongside it, so
    a reader never has to take this subtraction on trust.
    """
    volume = settled_gross_paise(report)
    unaccounted = report.total_unaccounted_paise
    return BatchView(
        run_id=report.audit_run_id,
        run_dir=report.run_dir,
        merchant_id=report.merchant_id,
        contract_version=report.contract_version,
        seed=report.seed,
        report_hash=report.report_hash,
        git_sha=report.git_sha,
        rounding_policy=report.rounding_policy,
        input_hash=report.input_hash,
        settled_gross=MoneyView.of(volume),
        verified=MoneyView.of(volume - abs(unaccounted)),
        unexplained=MoneyView.of(report.total_unexplained_paise),
        unclaimed=MoneyView.of(report.unclaimed_paise),
        unaccounted=MoneyView.of(unaccounted),
        unaccounted_bps_of_volume=_bps_of_volume(unaccounted, volume),
        record_count=report.record_count,
        credit_count=len(report.proofs),
        credits_resolved=sum(
            1 for p in report.proofs if p.outcome is DecompositionOutcome.RESOLVED
        ),
        finding_count=len(report.findings),
        cluster_count=len(report.clusters),
        journal_entry_count=len(report.journal_entries),
        contract_gap_count=len(report.contract_gaps),
        adjudication_degraded=report.adjudication_degraded,
        adjudication_degraded_reason=report.adjudication_degraded_reason,
        lanes=lane_buckets(report),
    )


class ClusterView(BaseModel):
    cluster_id: str
    discrepancy_class: str
    rule_id: str | None
    count: int
    impact: MoneyView
    share_of_unaccounted_bps: int


def cluster_views(report: AuditReport) -> list[ClusterView]:
    """Clusters ranked by rupee impact, descending.

    `cluster_findings` already sorts this way (core/exceptions.py), so the
    order is preserved rather than re-sorted -- the UI shows the engine's
    own ranking, not a second opinion about it.
    """
    total = sum(abs(c.total_impact.paise) for c in report.clusters)
    views = []
    for cluster in report.clusters:
        impact = cluster.total_impact.paise
        views.append(
            ClusterView(
                cluster_id=cluster.cluster_id,
                discrepancy_class=cluster.discrepancy_class.value,
                rule_id=cluster.rule_id,
                count=cluster.count,
                impact=MoneyView.of(impact),
                share_of_unaccounted_bps=(abs(impact) * 10_000 // total) if total else 0,
            )
        )
    return views


class EvidenceRef(BaseModel):
    type: str
    id: str


class FindingView(BaseModel):
    id: str
    discrepancy_class: str
    severity: str
    lane: str
    confidence_bps: int
    impact: MoneyView
    explanation: str
    evidence: list[EvidenceRef]


class ArithmeticView(BaseModel):
    rule_id: str | None
    rate_bps: int | None
    fixed_fee_paise: int | None
    representative_finding_id: str
    representative_delta: MoneyView


class ClusterDetail(BaseModel):
    cluster: ClusterView
    claim: str
    contract_clause: str | None
    narration: str | None
    narration_status: str
    arithmetic: ArithmeticView | None
    evidence: list[EvidenceRef]
    findings: list[FindingView]


def _finding_view(finding) -> FindingView:
    return FindingView(
        id=finding.id,
        discrepancy_class=finding.discrepancy_class.value,
        severity=finding.severity.value,
        lane=finding.lane.value,
        confidence_bps=finding.confidence,
        impact=MoneyView.of(finding.amount_impact.paise),
        explanation=finding.explanation,
        evidence=[EvidenceRef(type=r.type.value, id=r.id) for r in finding.evidence_ids],
    )


def cluster_detail(report: AuditReport, cluster_id: str) -> ClusterDetail | None:
    """One cluster with the dispute packet that prices it: the claim, the
    contract clause it rests on, the recomputed arithmetic, and every
    finding underneath. Returns None if the id is unknown, so the caller
    decides the HTTP status rather than this module raising."""
    cluster = next((c for c in report.clusters if c.cluster_id == cluster_id), None)
    if cluster is None:
        return None

    packet = next((p for p in report.dispute_packets if p.cluster_id == cluster_id), None)
    view = next(v for v in cluster_views(report) if v.cluster_id == cluster_id)
    by_id = {f.id: f for f in report.findings}
    findings = [_finding_view(by_id[fid]) for fid in cluster.finding_ids if fid in by_id]

    arithmetic = None
    if packet is not None and packet.recomputed_arithmetic is not None:
        a = packet.recomputed_arithmetic
        arithmetic = ArithmeticView(
            rule_id=a.rule_id,
            rate_bps=a.rate_bps,
            fixed_fee_paise=a.fixed_fee_paise,
            representative_finding_id=a.representative_finding_id,
            representative_delta=MoneyView.of(a.representative_delta_paise),
        )

    return ClusterDetail(
        cluster=view,
        claim=packet.claim if packet else "",
        contract_clause=packet.contract_clause if packet else None,
        narration=packet.narration if packet else None,
        narration_status=packet.narration_status if packet else "unavailable",
        arithmetic=arithmetic,
        evidence=[
            EvidenceRef(type=r.type.value, id=r.id) for r in (packet.evidence_ids if packet else [])
        ],
        findings=findings,
    )


class ScoreCard(BaseModel):
    """One headline ratio for the gauge cards.

    Every field is derived from the persisted report. The upstream card
    component this feeds generated its scores with `Utils.randomInt()`;
    nothing here is generated, and `detail` always names the two real
    quantities the ratio came from so a reader can check the division
    rather than trust the gauge.
    """

    key: str
    title: str
    description: str
    value_bps: int
    headline: str
    detail: str
    strength: str


def _strength(value_bps: int) -> str:
    """Same thresholds as the upstream card (80% / 40%), in integer bps."""
    if value_bps >= 8000:
        return "strong"
    if value_bps >= 4000:
        return "moderate"
    return "weak"


def _ratio_bps(numerator: int, denominator: int) -> int:
    """Integer basis points. A zero denominator is 'nothing to measure',
    reported as a full bar rather than a division."""
    if denominator == 0:
        return 10_000
    return max(0, min(10_000, numerator * 10_000 // denominator))


def _pct(value_bps: int) -> str:
    whole, frac = divmod(value_bps, 100)
    return f"{whole}.{frac:02d}%"


def score_cards(report: AuditReport) -> list[ScoreCard]:
    volume = settled_gross_paise(report)
    verified = volume - abs(report.total_unaccounted_paise)
    resolved = sum(1 for p in report.proofs if p.outcome is DecompositionOutcome.RESOLVED)
    clean = sum(1 for c in report.conservation if c.unexplained_paise == 0)

    verified_bps = _ratio_bps(verified, volume)
    decomposed_bps = _ratio_bps(resolved, len(report.proofs))
    clean_bps = _ratio_bps(clean, len(report.conservation))

    return [
        ScoreCard(
            key="verified_value",
            title="Verified value",
            description=(
                "Share of settled volume this audit accounted for, weighted by money "
                "rather than row count."
            ),
            value_bps=verified_bps,
            headline=_pct(verified_bps),
            detail=f"{rupees_display(verified)} of {rupees_display(volume)}",
            strength=_strength(verified_bps),
        ),
        ScoreCard(
            key="credits_decomposed",
            title="Credits decomposed",
            description=(
                "Credits resolved to an exact subset of transactions with a proof, "
                "not left ambiguous."
            ),
            value_bps=decomposed_bps,
            headline=_pct(decomposed_bps),
            detail=f"{resolved} of {len(report.proofs)} credits",
            strength=_strength(decomposed_bps),
        ),
        ScoreCard(
            key="clean_credits",
            title="Credits reconciled",
            description=(
                "Credits whose conservation identity closes with a zero residual — every "
                "paisa explained by a ledger record."
            ),
            value_bps=clean_bps,
            headline=_pct(clean_bps),
            detail=f"{clean} of {len(report.conservation)} credits",
            strength=_strength(clean_bps),
        ),
    ]


class TimelinePoint(BaseModel):
    """One bank credit, dated, for the settlement-month chart.

    `value_date` comes from the BankCredit record in the run directory,
    not from the report (ConservationReport carries only a credit_ref), so
    the chart's x-axis is the real date the money landed rather than an
    index the UI made up.
    """

    credit_id: str
    value_date: str
    credit: MoneyView
    settled_gross: MoneyView
    unexplained: MoneyView


def timeline_points(report: AuditReport, value_dates: dict[str, str]) -> list[TimelinePoint]:
    """Conservation rows joined to their credit's value_date, sorted by
    date. A credit whose date is missing from `value_dates` is dropped
    rather than dated to today -- an invented date on a settlement chart
    is a wrong fact, not a cosmetic gap."""
    points = [
        TimelinePoint(
            credit_id=c.credit_ref.id,
            value_date=value_dates[c.credit_ref.id],
            credit=MoneyView.of(c.credit_paise),
            settled_gross=MoneyView.of(c.settled_gross_paise),
            unexplained=MoneyView.of(c.unexplained_paise),
        )
        for c in report.conservation
        if c.credit_ref.id in value_dates
    ]
    points.sort(key=lambda p: (p.value_date, p.credit_id))
    return points


class ConservationView(BaseModel):
    """One bank credit's conservation identity, term by term, exactly as
    invariant 3 states it."""

    credit_id: str
    credit: MoneyView
    settled_gross: MoneyView
    refunds: MoneyView
    fees: MoneyView
    tax: MoneyView
    chargebacks: MoneyView
    adjustments: MoneyView
    reversals: MoneyView
    unexplained: MoneyView
    balances: bool


def conservation_views(report: AuditReport) -> list[ConservationView]:
    views = []
    for c in report.conservation:
        reconstructed = (
            c.settled_gross_paise
            - c.refunds_paise
            - c.fees_paise
            - c.tax_paise
            - c.chargebacks_paise
            - c.adjustments_paise
            + c.reversals_paise
            + c.unexplained_paise
        )
        views.append(
            ConservationView(
                credit_id=c.credit_ref.id,
                credit=MoneyView.of(c.credit_paise),
                settled_gross=MoneyView.of(c.settled_gross_paise),
                refunds=MoneyView.of(c.refunds_paise),
                fees=MoneyView.of(c.fees_paise),
                tax=MoneyView.of(c.tax_paise),
                chargebacks=MoneyView.of(c.chargebacks_paise),
                adjustments=MoneyView.of(c.adjustments_paise),
                reversals=MoneyView.of(c.reversals_paise),
                unexplained=MoneyView.of(c.unexplained_paise),
                # Recomputed here rather than trusted: conserve.py already
                # asserts this at audit time, and a UI that simply restated
                # the claim would be decoration. If this is ever False the
                # screen should say so loudly.
                balances=reconstructed == c.credit_paise,
            )
        )
    return views
