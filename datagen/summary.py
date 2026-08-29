"""Summary statistics for CLI stdout after a generation run."""

from __future__ import annotations

from collections import Counter

from datagen.ground_truth import GroundTruth
from datagen.world import World


def build_summary(world: World, ground_truth: GroundTruth) -> dict:
    payment_settlement = {p.id: p.settlement_id for p in world.payments}
    cross_cycle_refunds = sum(1 for r in world.refunds if payment_settlement.get(r.payment_id) != r.settlement_id)

    return {
        "payments": {
            "total": len(world.payments),
            "by_method": dict(Counter(p.method.value for p in world.payments)),
            "by_card_type": dict(Counter(p.card_type.value for p in world.payments if p.card_type is not None)),
            "international": sum(1 for p in world.payments if p.is_international),
            "gross_paise": sum(p.amount.paise for p in world.payments),
        },
        "refunds": {
            "total": len(world.refunds),
            "partial": sum(1 for r in world.refunds if r.is_partial),
            "full": sum(1 for r in world.refunds if not r.is_partial),
            "cross_cycle": cross_cycle_refunds,
            "total_paise": sum(r.amount.paise for r in world.refunds),
        },
        "chargebacks": {
            "total": len(world.chargebacks),
            "by_stage": dict(Counter(c.stage.value for c in world.chargebacks)),
            "total_paise": sum(c.amount.paise for c in world.chargebacks),
        },
        "adjustments": {
            "total": len(world.adjustments),
            "by_kind": dict(Counter(a.kind.value for a in world.adjustments)),
        },
        "fees": {
            "count": len(world.fee_lines),
            "total_paise": sum(f.computed_amount.paise for f in world.fee_lines),
        },
        "tax": {
            "count": len(world.tax_lines),
            "total_paise": sum(t.amount.paise for t in world.tax_lines),
        },
        "batches": len(world.batches),
        "bank_credits": len(world.bank_credits),
        "total_credited_paise": sum(b.expected_credit.paise for b in world.batches),
        "ground_truth": {
            "discrepancies": len(ground_truth.discrepancies),
            "data_quality_flags": len(ground_truth.data_quality_flags),
            "silent_corruption": ground_truth.silent_corruption is not None,
            "counts": ground_truth.counts,
        },
    }


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}Rs.{whole:,}.{frac:02d}"


def render_summary(summary: dict) -> str:
    p = summary["payments"]
    r = summary["refunds"]
    c = summary["chargebacks"]
    a = summary["adjustments"]
    gt = summary["ground_truth"]

    lines = [
        f"Payments: {p['total']} (international: {p['international']}), gross {_rupees(p['gross_paise'])}",
        f"  by method: {p['by_method']}",
        f"  by card type: {p['by_card_type']}",
        (
            f"Refunds: {r['total']} (partial: {r['partial']}, full: {r['full']}, "
            f"cross-cycle: {r['cross_cycle']}), total {_rupees(r['total_paise'])}"
        ),
        f"Chargebacks: {c['total']} {c['by_stage']}, total {_rupees(c['total_paise'])}",
        f"Adjustments: {a['total']} {a['by_kind']}",
        f"Fees: {summary['fees']['count']} lines, total {_rupees(summary['fees']['total_paise'])}",
        f"Tax: {summary['tax']['count']} lines, total {_rupees(summary['tax']['total_paise'])}",
        f"Settlement batches: {summary['batches']}, Bank credits: {summary['bank_credits']}",
        f"Total credited: {_rupees(summary['total_credited_paise'])}",
        (
            f"Ground truth: {gt['discrepancies']} discrepancies, "
            f"{gt['data_quality_flags']} data-quality flags, "
            f"silent_corruption={gt['silent_corruption']}"
        ),
        f"  counts by class: {gt['counts']}",
    ]
    return "\n".join(lines)
