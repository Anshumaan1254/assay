"""Bake `site/src/data/audit.json` from a signed audit report and the eval sweep.

The landing page is static and ships to GitHub Pages with no Python behind
it, so every figure it prints has to be frozen into a file at build time.
This is the only place that happens, and it reads exactly two sources:

  * `runs/realistic-seed42/report.json` -- the committed reference audit,
    carrying its own `report_hash` over its canonical JSON (invariant 4).
  * `eval/results/sweep.json` -- the machine-readable sweep behind
    EVIDENCE.md. EVIDENCE.md itself is prose generated from this file, so
    parsing the markdown would be reading a rendering of the truth rather
    than the truth.

Ground truth is never touched: `datagen/` is not imported here, and the
sweep's planted totals are read as already-aggregated metrics, which is what
`eval/` exists to publish (invariant 5).

Every amount leaves here twice -- the exact integer paise the engine
computed, and the string the browser should print. The browser divides
nothing by 100 and adds nothing up; see `tests/test_site.py`, which fails if
this file's identity does not balance in integer paise.

    python scripts/bake_site_data.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reviewer.derive import rupees, rupees_display

REPORT = ROOT / "runs" / "realistic-seed42" / "report.json"
SWEEP = ROOT / "eval" / "results" / "sweep.json"
OUT = ROOT / "site" / "src" / "data" / "audit.json"

# The eight terms of invariant 3, in the order the identity states them.
# `op` is how each term enters the sum; the site renders it verbatim rather
# than deciding any sign for itself.
TERMS: list[tuple[str, str, str, str]] = [
    ("settled_gross", "settled gross", "", "Every rupee the gateway says it settled."),
    ("refunds", "refunds", "-", "Money returned to the cardholder."),
    ("fees", "fees", "-", "MDR, per-method flat fees, caps -- recomputed from the rate card."),
    ("tax", "tax", "-", "GST on the fee, recomputed from the recomputed fee."),
    ("chargebacks", "chargebacks", "-", "Disputed transactions clawed back."),
    ("adjustments", "adjustments", "-", "Reserve holds and other gateway-side movements."),
    ("reversals", "reversals", "+", "Holds released back to the merchant."),
    ("unexplained", "unexplained", "+", "The residual. First-class, never rounded away."),
]


def money(paise: int) -> dict[str, Any]:
    """One amount, three ways: exact, plain, and grouped for display."""
    return {"paise": paise, "rupees": rupees(paise), "display": rupees_display(paise)}


def shard_counts(weights: dict[str, int], total_shards: int) -> dict[str, int]:
    """How many of `total_shards` cubes belong in each bin.

    A visual allocation, not an amount: it decides where a cube flies, and no
    rupee figure is ever derived from it. Done in integer arithmetic anyway,
    by largest remainder so the parts sum to the whole exactly -- a bin one
    cube short of its share looks like a rounding bug to precisely the reader
    this page is written for.
    """
    magnitudes = {key: abs(value) for key, value in weights.items()}
    total_weight = sum(magnitudes.values())
    if total_weight == 0:
        raise SystemExit("bake: every conservation term is zero; nothing to allocate")

    scaled = {key: weight * total_shards for key, weight in magnitudes.items()}
    counts = {key: value // total_weight for key, value in scaled.items()}
    remainders = sorted(
        ((scaled[key] % total_weight, key) for key in magnitudes),
        key=lambda pair: (-pair[0], pair[1]),
    )
    for _, key in remainders[: total_shards - sum(counts.values())]:
        counts[key] += 1

    if sum(counts.values()) != total_shards:
        raise SystemExit("bake: shard allocation lost a cube")
    return counts


def build() -> dict[str, Any]:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    sweep = json.loads(SWEEP.read_text(encoding="utf-8"))

    rows = report["conservation"]

    def across(field: str) -> int:
        return sum(row[field] for row in rows)

    credit_paise = across("credit_paise")
    term_paise = {key: across(f"{key}_paise") for key, _, _, _ in TERMS}

    # Invariant 3, restated here rather than trusted. If the report the site
    # is about to quote does not balance, the site does not get built.
    reconstructed = (
        term_paise["settled_gross"]
        - term_paise["refunds"]
        - term_paise["fees"]
        - term_paise["tax"]
        - term_paise["chargebacks"]
        - term_paise["adjustments"]
        + term_paise["reversals"]
        + term_paise["unexplained"]
    )
    if reconstructed != credit_paise:
        raise SystemExit(
            f"bake: conservation does not hold in {REPORT}: "
            f"{reconstructed} != {credit_paise} paise"
        )

    ledger = json.loads((REPORT.parent / "ledger.json").read_text(encoding="utf-8"))
    payments = len(ledger["payments"])

    # The seven terms that receive cubes. `unexplained` is deliberately not a
    # bin: it is what is left over once every bin has been filled, which is
    # the whole point of the last beat.
    shards = shard_counts({k: v for k, v in term_paise.items() if k != "unexplained"}, payments)

    tiers: dict[str, int] = {}
    for proof in report["proofs"]:
        tiers[proof["tier"]] = tiers.get(proof["tier"], 0) + 1

    lanes: dict[str, int] = {"auto": 0, "propose": 0, "escalate": 0}
    for finding in report["findings"]:
        lanes[finding["lane"]] = lanes.get(finding["lane"], 0) + 1

    detection = sweep["detection"]
    determinism = sweep["determinism"]
    throughput = sweep["throughput"]
    llm = sweep["llm"]
    support = sum(row["support"] for row in detection["code_rows"])

    return {
        "_generated_by": "scripts/bake_site_data.py -- do not edit by hand",
        "provenance": {
            "run_id": REPORT.parent.name,
            "audit_run_id": report["audit_run_id"],
            "report_hash": report["report_hash"],
            "input_hash": report["input_hash"],
            "contract_version": report["contract_version"],
            "calibration_sha256": report["calibration_sha256"],
            "git_sha": report["git_sha"],
            "seed": report["seed"],
            "rounding_policy": report["rounding_policy"],
            "record_count": report["record_count"],
        },
        "credit": money(credit_paise),
        "credits": len(rows),
        # Everything the gateway kept: settled gross less what reached the
        # bank. Baked rather than subtracted in the browser, and carried
        # with its share of volume so the page can make the point that the
        # entire audit is about a few percent of the money.
        "gap": {
            **money(term_paise["settled_gross"] - credit_paise),
            "share_pct": round(
                100 * (term_paise["settled_gross"] - credit_paise) / term_paise["settled_gross"],
                2,
            ),
        },
        "terms": [
            {
                "key": key,
                "label": label,
                "op": op,
                "note": note,
                "shards": shards.get(key, 0),
                **money(term_paise[key]),
            }
            for key, label, op, note in TERMS
        ],
        "shards": {
            "total": payments,
            "records": report["record_count"],
            "refunds": len(ledger["refunds"]),
            "chargebacks": len(ledger["chargebacks"]),
            "adjustments": len(ledger["adjustments"]),
        },
        "unaccounted": {
            "unexplained": money(report["total_unexplained_paise"]),
            "unclaimed": money(report["unclaimed_paise"]),
            "total": money(report["total_unaccounted_paise"]),
            "unclaimed_ids": [entry["id"] for entry in report["unclaimed"]],
        },
        "decomposition": {
            "credits": len(rows),
            "resolved": sum(1 for proof in report["proofs"] if proof["outcome"] == "resolved"),
            "structural": tiers.get("structural", 0),
            "subset_sum": tiers.get("subset_sum", 0),
            "assignment": tiers.get("assignment", 0),
        },
        "findings": {
            "count": len(report["findings"]),
            "clusters": len(report["clusters"]),
            "lanes": lanes,
        },
        "evidence": {
            "months": len(sweep["runs"]),
            "findings_total": detection["findings_total"],
            "false_positives": detection["false_positive_findings"],
            "falsely_claimed": money(detection["false_positive_paise"]),
            "planted": money(detection["planted_paise"]),
            "detected": money(detection["detected_paise"]),
            "value_recall_pct": round(
                100 * detection["detected_paise"] / detection["planted_paise"], 1
            ),
            "count_recall_pct": round(
                100 * detection["true_positive_findings"] / support, 1
            ),
            "records_total": throughput["records"],
            "records_per_second": round(throughput["records_per_second"]),
            "records_touching_a_model": llm["records_touching_a_model"],
            "schema_rejections": llm["schema_rejections"],
            "citations_rejected": (
                llm["citations_rejected_nonexistent"] + llm["citations_rejected_not_in_pool"]
            ),
            "determinism_matches": bool(determinism["matches"]),
            "replay_matches": bool(determinism["replay_matches"]),
            "replay_live_calls": determinism["replay_made_live_calls"],
            "clean_profile_unaccounted": money(sweep["clean_profile_unaccounted_paise"]),
            "auto_lane_certified": sweep["conformal"]["auto_min_calibrated_bps"] is not None,
        },
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = build()
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # `rupees`, not `display`: the grouped form carries a rupee sign, and a
    # Windows console on cp1252 cannot encode it. The file itself is UTF-8.
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  credit      INR {payload['credit']['rupees']} over {payload['credits']} credits")
    print(f"  shards      {payload['shards']['total']}")
    print(f"  unaccounted INR {payload['unaccounted']['total']['rupees']}")
    print(f"  report_hash {payload['provenance']['report_hash']}")


if __name__ == "__main__":
    main()
