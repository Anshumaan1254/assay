/* Typed client for the reviewer API. Mirrors reviewer/derive.py's response
 * models. Money always arrives as { paise, rupees } — the exact integer and
 * the string to print — so nothing here ever divides by 100. */

export interface MoneyView {
  paise: number;
  rupees: string;
}

export interface LaneBucket {
  lane: "auto" | "propose" | "escalate";
  finding_count: number;
  impact: MoneyView;
  posts_automatically: boolean;
  description: string;
}

export interface BatchView {
  run_id: string;
  run_dir: string;
  merchant_id: string;
  contract_version: string;
  seed: number;
  report_hash: string;
  git_sha: string;
  rounding_policy: string;
  input_hash: string;
  settled_gross: MoneyView;
  verified: MoneyView;
  unexplained: MoneyView;
  unclaimed: MoneyView;
  unaccounted: MoneyView;
  unaccounted_bps_of_volume: number;
  record_count: number;
  credit_count: number;
  credits_resolved: number;
  finding_count: number;
  cluster_count: number;
  journal_entry_count: number;
  contract_gap_count: number;
  adjudication_degraded: boolean;
  adjudication_degraded_reason: string | null;
  lanes: LaneBucket[];
}

export interface ClusterView {
  cluster_id: string;
  discrepancy_class: string;
  rule_id: string | null;
  count: number;
  impact: MoneyView;
  share_of_unaccounted_bps: number;
}

export interface EvidenceRef {
  type: string;
  id: string;
}

export interface FindingView {
  id: string;
  discrepancy_class: string;
  severity: string;
  lane: "auto" | "propose" | "escalate";
  confidence_bps: number;
  impact: MoneyView;
  explanation: string;
  evidence: EvidenceRef[];
}

export interface ArithmeticView {
  rule_id: string | null;
  rate_bps: number | null;
  fixed_fee_paise: number | null;
  representative_finding_id: string;
  representative_delta: MoneyView;
}

export interface ClusterDetail {
  cluster: ClusterView;
  claim: string;
  contract_clause: string | null;
  narration: string | null;
  narration_status: string;
  arithmetic: ArithmeticView | null;
  evidence: EvidenceRef[];
  findings: FindingView[];
}

export interface ConservationView {
  credit_id: string;
  credit: MoneyView;
  settled_gross: MoneyView;
  refunds: MoneyView;
  fees: MoneyView;
  tax: MoneyView;
  chargebacks: MoneyView;
  adjustments: MoneyView;
  reversals: MoneyView;
  unexplained: MoneyView;
  balances: boolean;
}

export interface RunSummary {
  run_id: string;
  started_at: string;
  contract_version: string;
  seed: number;
  report_hash: string;
  run_dir: string;
  available: boolean;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* a non-JSON error body is still an error; keep the status line */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  runs: () => get<RunSummary[]>("/api/runs"),
  batch: (runId: string) => get<BatchView>(`/api/runs/${encodeURIComponent(runId)}`),
  clusters: (runId: string) => get<ClusterView[]>(`/api/runs/${encodeURIComponent(runId)}/clusters`),
  cluster: (runId: string, clusterId: string) =>
    get<ClusterDetail>(
      `/api/runs/${encodeURIComponent(runId)}/clusters/${encodeURIComponent(clusterId)}`,
    ),
  conservation: (runId: string) =>
    get<ConservationView[]>(`/api/runs/${encodeURIComponent(runId)}/conservation`),
  explain: (runId: string, recordId: string) =>
    get<{ record_id: string; text: string }>(
      `/api/runs/${encodeURIComponent(runId)}/explain/${encodeURIComponent(recordId)}`,
    ),
};
