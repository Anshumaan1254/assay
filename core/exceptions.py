"""Taxonomy, clustering, pricing.

Adding a `DiscrepancyClass` member is one of five coordinated steps — see
.claude/skills/new-discrepancy-class/SKILL.md. Do all five or none.

The clustering/pricing/dispute-packet code below turns a run's raw
`Finding`s into "37 rows, one cause, Rs 12,400" -- the exception report is
priced and ranked by money, never left as N unranked rows. This file stays
`core/`: fully deterministic, no `llm/` import, no float. A dispute
packet's prose narration is filled in afterwards by `llm/narrator.py`,
which imports `core.exceptions` (legal) rather than the reverse.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from core.contract import CompiledContract
    from core.ledger import Ledger
    from core.models import Finding


class DiscrepancyClass(StrEnum):
    """The settlement discrepancies this engine can name.

    Each member is a real-world cause, not a symptom — `core/verify.py`'s
    detection rules and `datagen/inject.py`'s planted-truth injectors both
    key off these values, so they must stay stable once in use.
    """

    FEE_OVERCHARGE = "fee_overcharge"
    """Gateway charged more fee (MDR/fixed/international/EMI) than the
    contracted rate card produces for that transaction."""

    FEE_UNDERCHARGE = "fee_undercharge"
    """Gateway charged less fee than the contract specifies — favorable to
    the merchant, still reported: it signals a stale or misapplied rate."""

    TAX_MISCALCULATION = "tax_miscalculation"
    """Tax on a fee line does not equal rate_bps * base_amount for that
    line, e.g. GST applied at the wrong slab or against the wrong base."""

    MISSING_TRANSACTION = "missing_transaction"
    """A payment present in the merchant ledger for this cycle never
    appears in the settlement report or bank credit."""

    DUPLICATE_SETTLEMENT = "duplicate_settlement"
    """The same payment was credited more than once across settlement
    batches or bank credits."""

    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"
    """The refund amount deducted in the settlement does not match the
    Refund record's amount on the merchant ledger."""

    CHARGEBACK_AMOUNT_MISMATCH = "chargeback_amount_mismatch"
    """The chargeback deduction, or reversal on a won dispute, does not
    match the ledger's Chargeback amount and stage."""

    UNRELEASED_RESERVE = "unreleased_reserve"
    """A reserve_hold adjustment has no matching reserve_release after the
    contracted hold period."""

    UNDOCUMENTED_ADJUSTMENT = "undocumented_adjustment"
    """A manual_credit, manual_debit, or fee_waiver adjustment appears in
    the settlement with no reason traceable to a ledger record or contract
    clause."""

    FX_MARKUP_VARIANCE = "fx_markup_variance"
    """For an is_international payment, the currency-conversion markup
    charged does not match the contracted FX rate."""

    ROUNDING_DRIFT = "rounding_drift"
    """Sub-paisa rounding choices accumulate to a small non-zero difference
    that no single fee or tax line explains on its own."""

    UNRECONCILED_RESIDUAL = "unreconciled_residual"
    """After every other class has been checked, a non-zero amount remains
    in the conservation identity's `unexplained` bucket (invariant 3)."""


# core/models.py imports DiscrepancyClass (just above) for Finding's own
# field type, so these imports must come after that class is fully defined
# -- otherwise loading core.models first (the common case, since most
# modules reach core.exceptions via core.contract/core.decompose) would hit
# a genuine cycle: models -> exceptions -> models, with core.models only
# partially initialized. See core/models.py's own comment at its Finding
# import for the other half of this.
from core.models import AssayModel, EntityType, PaisaAmount, RecordRef
from core.money import Money

# ---------------------------------------------------------------------------
# Clustering + pricing
#
# A cluster key is (discrepancy_class, rule_id, delta_bucket). rule_id is
# resolved from a finding's own evidence_ids -- the contract clause actually
# misapplied, if any is traceable. delta_bucket groups "the same clause
# misapplied at slightly different amounts" into one cluster while still
# separating two genuinely different bugs that happen to share a rule_id:
# the finding's amount_impact expressed as integer bps of its first
# resolvable payment's gross, rounded to the nearest 50 bps. Both fall back
# to None when nothing more specific is resolvable (e.g. a chargeback
# finding, whose only evidence is the Chargeback itself) -- those findings
# still cluster, by class alone, which is the honest answer given nothing
# more is knowable.
# ---------------------------------------------------------------------------

_DELTA_BUCKET_WIDTH_BPS = 50


def _rule_id_for_finding(finding: Finding, ledger: Ledger) -> str | None:
    for ref in finding.evidence_ids:
        if ref.type is EntityType.FEE_LINE:
            fee_line = ledger.get(ref)
            if fee_line is not None:
                return fee_line.rule_id
    for ref in finding.evidence_ids:
        if ref.type is EntityType.TAX_LINE:
            tax_line = ledger.get(ref)
            if tax_line is None:
                continue
            fee_ref = RecordRef(type=EntityType.FEE_LINE, id=tax_line.applies_to_fee_id)
            fee_line = ledger.get(fee_ref)
            if fee_line is not None:
                return fee_line.rule_id
    return None


def _delta_bucket_bps(finding: Finding, ledger: Ledger) -> int | None:
    for ref in finding.evidence_ids:
        if ref.type is not EntityType.PAYMENT:
            continue
        payment = ledger.get(ref)
        if payment is None or payment.amount.paise == 0:
            continue
        raw_bps = finding.amount_impact.paise * 10_000 // payment.amount.paise
        return (raw_bps + _DELTA_BUCKET_WIDTH_BPS // 2) // _DELTA_BUCKET_WIDTH_BPS * _DELTA_BUCKET_WIDTH_BPS
    return None


class Cluster(AssayModel):
    """One root cause, priced. `count` findings collapse into one row here
    -- "37 rows, one cause, Rs 12,400" rather than 37 lines."""

    cluster_id: str
    discrepancy_class: DiscrepancyClass
    rule_id: str | None
    finding_ids: list[str]
    total_impact: PaisaAmount
    count: int


def _cluster_id(audit_run_id: str, discrepancy_class: DiscrepancyClass, rule_id: str | None, bucket: int | None) -> str:
    return (
        f"CLU-{audit_run_id}-{discrepancy_class.value}-"
        f"{rule_id if rule_id is not None else 'none'}-"
        f"{bucket if bucket is not None else 'none'}"
    )


def cluster_findings(findings: Sequence[Finding], ledger: Ledger, *, audit_run_id: str) -> list[Cluster]:
    """Group findings sharing a root cause, price each group in rupees, and
    rank by impact -- the exception report sorts by money, never row order.
    Deterministic clustering rule, not a model."""
    groups: dict[tuple[DiscrepancyClass, str | None, int | None], list[Finding]] = {}
    for finding in findings:
        key = (
            finding.discrepancy_class,
            _rule_id_for_finding(finding, ledger),
            _delta_bucket_bps(finding, ledger),
        )
        groups.setdefault(key, []).append(finding)

    clusters = [
        Cluster(
            cluster_id=_cluster_id(audit_run_id, discrepancy_class, rule_id, bucket),
            discrepancy_class=discrepancy_class,
            rule_id=rule_id,
            finding_ids=sorted(f.id for f in group),
            total_impact=Money(sum(f.amount_impact.paise for f in group)),
            count=len(group),
        )
        for (discrepancy_class, rule_id, bucket), group in groups.items()
    ]
    return sorted(clusters, key=lambda c: (-c.total_impact.paise, c.cluster_id))


# ---------------------------------------------------------------------------
# Dispute packets
#
# Fully deterministic assembly -- core/ never calls llm/. `narration` and
# `narration_status` are filled in afterwards by llm/narrator.py, which
# imports this module (legal: llm/ may import core/, never the reverse).
# ---------------------------------------------------------------------------


class RecomputedArithmetic(AssayModel):
    """The contract's own formula, plus one worked example. Not all N
    findings' arithmetic repeated -- that's what evidence_ids/finding_ids
    are for -- just enough to show the rule and prove it against one case."""

    rule_id: str | None
    rate_bps: int | None
    fixed_fee_paise: int | None
    representative_finding_id: str
    representative_delta_paise: int


class DisputePacket(AssayModel):
    cluster_id: str
    discrepancy_class: DiscrepancyClass
    claim: str
    contract_clause: str | None
    recomputed_arithmetic: RecomputedArithmetic | None
    evidence_ids: list[RecordRef]
    finding_ids: list[str]
    total_impact: PaisaAmount
    narration: str | None = None
    # Fixed vocabulary, never "unknown error": a packet that hasn't been
    # narrated yet, or whose narration call failed, says so structurally.
    narration_status: Literal["generated", "unavailable"] = "unavailable"


def _clause_for_rule(rule_id: str | None, contract: CompiledContract) -> str | None:
    if rule_id is None:
        return None
    for rule in contract.rules:
        if rule.rule_id == rule_id:
            return rule.source_quote
    return None


def _arithmetic_for(rule_id: str | None, findings: Sequence[Finding], contract: CompiledContract) -> RecomputedArithmetic | None:
    if rule_id is None or not findings:
        return None
    representative = findings[0]
    rate_bps: int | None = None
    fixed_fee_paise: int | None = None
    for rule in contract.rules:
        if rule.rule_id == rule_id:
            rate_bps = rule.rate_bps
            fixed_fee_paise = rule.fixed_fee_paise
            break
    return RecomputedArithmetic(
        rule_id=rule_id,
        rate_bps=rate_bps,
        fixed_fee_paise=fixed_fee_paise,
        representative_finding_id=representative.id,
        representative_delta_paise=representative.amount_impact.paise,
    )


def build_dispute_packet(cluster: Cluster, findings: Sequence[Finding], contract: CompiledContract) -> DisputePacket:
    """The claim, the contract clause text, the recomputed arithmetic, and
    the full evidence list for one cluster. Narration is added afterwards
    by llm/narrator.py -- numbers injected, never generated."""
    cluster_findings_ = [f for f in findings if f.id in set(cluster.finding_ids)]
    evidence_ids = sorted(
        {ref for f in cluster_findings_ for ref in f.evidence_ids}, key=lambda r: (r.type.value, r.id)
    )
    claim = (
        f"{cluster.total_impact.to_rupees_str()} {cluster.discrepancy_class.value} "
        f"across {cluster.count} finding{'s' if cluster.count != 1 else ''}"
    )
    return DisputePacket(
        cluster_id=cluster.cluster_id,
        discrepancy_class=cluster.discrepancy_class,
        claim=claim,
        contract_clause=_clause_for_rule(cluster.rule_id, contract),
        recomputed_arithmetic=_arithmetic_for(cluster.rule_id, cluster_findings_, contract),
        evidence_ids=evidence_ids,
        finding_ids=list(cluster.finding_ids),
        total_impact=cluster.total_impact,
    )
