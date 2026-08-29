/* The audit, as the page is allowed to know it.
 *
 * `audit.json` is baked by `scripts/bake_site_data.py` from a signed
 * `report.json` and the eval sweep. Every amount arrives as the exact
 * integer paise the engine computed plus the string to print, so nothing
 * here ever divides by 100, sums a column, or otherwise does money
 * arithmetic in IEEE 754 -- the same position `reviewer/` takes, for the
 * same reason. If a figure is not in this file, the page does not show it.
 */

import data from "../data/audit.json";

export interface MoneyView {
  /** Exact, as the engine computed it. Present so the number is auditable
   *  in the DOM even though the page renders `display`. */
  paise: number;
  /** Ungrouped, e.g. "6167971.02". */
  rupees: string;
  /** Grouped and symbol-prefixed, e.g. "₹61,67,971.02". What gets shown. */
  display: string;
}

export interface Term extends MoneyView {
  key: string;
  label: string;
  /** "", "-" or "+" -- how this term enters the identity. Rendered
   *  verbatim; the page decides no signs of its own. */
  op: string;
  note: string;
  /** Cubes allocated to this bin. A visual quantity, never an amount. */
  shards: number;
}

export interface Audit {
  provenance: {
    run_id: string;
    audit_run_id: string;
    report_hash: string;
    input_hash: string;
    contract_version: string;
    calibration_sha256: string;
    git_sha: string;
    seed: number;
    rounding_policy: string;
    record_count: number;
  };
  credit: MoneyView;
  credits: number;
  /** Settled gross less what reached the bank -- everything the gateway
   *  deducted, and its share of volume. */
  gap: MoneyView & { share_pct: number };
  terms: Term[];
  shards: {
    total: number;
    records: number;
    refunds: number;
    chargebacks: number;
    adjustments: number;
  };
  unaccounted: {
    unexplained: MoneyView;
    unclaimed: MoneyView;
    total: MoneyView;
    unclaimed_ids: string[];
  };
  decomposition: {
    credits: number;
    resolved: number;
    structural: number;
    subset_sum: number;
    assignment: number;
  };
  findings: {
    count: number;
    clusters: number;
    lanes: { auto: number; propose: number; escalate: number };
  };
  evidence: {
    months: number;
    findings_total: number;
    false_positives: number;
    falsely_claimed: MoneyView;
    planted: MoneyView;
    detected: MoneyView;
    value_recall_pct: number;
    count_recall_pct: number;
    records_total: number;
    records_per_second: number;
    records_touching_a_model: number;
    schema_rejections: number;
    citations_rejected: number;
    determinism_matches: boolean;
    replay_matches: boolean;
    replay_live_calls: number;
    clean_profile_unaccounted: MoneyView;
    auto_lane_certified: boolean;
  };
}

export const audit = data as unknown as Audit;

/** Thousands separators for a plain count. Counts are not money -- this is
 *  `Intl` on an integer, and it is never applied to a rupee figure. */
export const count = (value: number) => value.toLocaleString("en-IN");

/** The terms that receive cubes, in identity order. `unexplained` is not a
 *  bin: it is what is left when every bin is full. */
export const binTerms = audit.terms.filter((term) => term.key !== "unexplained");

export const REPO = "https://github.com/Anshumaan1254/assay";

/** The reviewer is a FastAPI app over a computed audit, so there is no
 *  static URL to send a reader to -- this points at its source and the one
 *  command that starts it. `reviewer/` itself is untouched by this page. */
export const REVIEWER = `${REPO}/tree/main/reviewer`;
