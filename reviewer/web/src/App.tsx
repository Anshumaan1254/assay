import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

import {
  api,
  type BatchView,
  type ClusterDetail,
  type ClusterView,
  type ConservationView,
  type MoneyView,
  type RunSummary,
  type ScoreCard,
  type TimelinePoint,
} from "./api";
import { FinancialScoreCards } from "@/components/ui/financial-score-cards";
import { Component as AreaChart } from "@/components/ui/finance-chart";
import ScrollExpandMedia from "@/components/ui/scroll-expansion-hero";

gsap.registerPlugin(ScrollTrigger);

const REDUCED = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ── money formatting ───────────────────────────────────────────────────
 * The server sends the exact integer paise and its own ungrouped rupee
 * string. Grouping is applied here for legibility only, by string surgery
 * on the digits — never by parsing the amount into a float. */

function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${last3}`;
}

function fmtRupees(rupees: string): string {
  const negative = rupees.startsWith("-");
  const body = negative ? rupees.slice(1) : rupees;
  const [whole, frac = "00"] = body.split(".");
  return `${negative ? "−" : ""}${groupIndian(whole)}.${frac}`;
}

function fmtPaise(paise: number): string {
  const negative = paise < 0;
  const abs = Math.abs(Math.round(paise));
  const whole = Math.floor(abs / 100).toString();
  const frac = (abs % 100).toString().padStart(2, "0");
  return `${negative ? "−" : ""}${groupIndian(whole)}.${frac}`;
}

const money = (m: MoneyView) => fmtRupees(m.rupees);

/* ── shell states ───────────────────────────────────────────────────── */

function Loading() {
  return (
    <div className="center">
      <div>
        <div className="spinner" />
        <p className="mono" style={{ color: "var(--ink-faint)", letterSpacing: "0.16em" }}>
          READING REPORT
        </p>
      </div>
    </div>
  );
}

function Failure({ message }: { message: string }) {
  return (
    <div className="center">
      <div className="err">
        <h1>No audit to review</h1>
        <p>{message}</p>
        <p style={{ marginTop: "1rem" }}>
          The reviewer reads runs the engine has already produced. Generate one and audit it, then
          reload:
        </p>
        <code>
          assay generate --profile realistic --seed 42
          <br />
          assay audit --run-dir runs/realistic-seed42
        </code>
      </div>
    </div>
  );
}

/* ── hero ───────────────────────────────────────────────────────────── */

function Hero({ batch }: { batch: BatchView }) {
  const figureRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLElement>(null);
  const clean = batch.unaccounted.paise === 0;

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      const target = figureRef.current?.querySelector<HTMLElement>("[data-count]");
      if (target && !REDUCED()) {
        const value = { v: 0 };
        gsap.to(value, {
          v: batch.unaccounted.paise,
          duration: 1.9,
          ease: "expo.out",
          delay: 0.25,
          onUpdate: () => {
            target.textContent = fmtPaise(value.v);
          },
          onComplete: () => {
            target.textContent = money(batch.unaccounted);
          },
        });
      }

      gsap.from(".hero__label, .hero__verdict, .scrollcue", {
        y: 18,
        opacity: 0,
        duration: 0.9,
        ease: "power3.out",
        stagger: 0.09,
        delay: 0.1,
      });
      gsap.from(".hero__cell", {
        y: 22,
        opacity: 0,
        duration: 0.8,
        ease: "power3.out",
        stagger: 0.06,
        delay: 0.5,
      });
      gsap.to(".scrollcue__line", {
        scaleX: 0.25,
        repeat: -1,
        yoyo: true,
        duration: 1.1,
        ease: "sine.inOut",
      });

      // The hero recedes as the page moves past it, so the eye is handed
      // to the identity section rather than competing with it.
      gsap.to(rootRef.current, {
        opacity: 0.12,
        y: -60,
        ease: "none",
        scrollTrigger: {
          trigger: rootRef.current,
          start: "top top",
          end: "bottom top",
          scrub: 0.6,
        },
      });
    }, rootRef);
    return () => ctx.revert();
  }, [batch]);

  return (
    <section className="hero" ref={rootRef}>
      <p className="hero__label">Unaccounted after independent recomputation</p>
      <div className={`hero__figure ${clean ? "is-clean" : "is-dirty"}`} ref={figureRef}>
        <span className="rupee">₹</span>
        <span data-count>{money(batch.unaccounted)}</span>
      </div>
      <p className="hero__verdict">
        {clean ? (
          <>
            Every rupee of the <strong>₹{money(batch.settled_gross)}</strong> settled in this batch
            was decomposed into the transactions that produced it and independently recomputed
            against the contracted rate card. Nothing is missing.
          </>
        ) : (
          <>
            Of <strong>₹{money(batch.settled_gross)}</strong> settled across {batch.credit_count}{" "}
            bank credits, this amount cannot be explained by any transaction, fee, tax or
            adjustment on record — {batch.unaccounted_bps_of_volume} bps of volume, across{" "}
            <strong>{batch.finding_count}</strong> findings in {batch.cluster_count} clusters.
          </>
        )}
      </p>

      <dl className="hero__strip">
        <div className="hero__cell">
          <dt>Settled gross</dt>
          <dd>₹{money(batch.settled_gross)}</dd>
        </div>
        <div className="hero__cell">
          <dt>Verified</dt>
          <dd style={{ color: "var(--green)" }}>₹{money(batch.verified)}</dd>
        </div>
        <div className="hero__cell">
          <dt>Unexplained</dt>
          <dd style={{ color: batch.unexplained.paise ? "var(--red)" : undefined }}>
            ₹{money(batch.unexplained)}
          </dd>
        </div>
        <div className="hero__cell">
          <dt>Unclaimed</dt>
          <dd style={{ color: batch.unclaimed.paise ? "var(--red)" : undefined }}>
            ₹{money(batch.unclaimed)}
          </dd>
        </div>
        <div className="hero__cell">
          <dt>Credits resolved</dt>
          <dd>
            {batch.credits_resolved}/{batch.credit_count}
          </dd>
        </div>
      </dl>

      <div className="scrollcue">
        <span className="scrollcue__line" />
        <span>Scroll — the identity, term by term</span>
      </div>
    </section>
  );
}

/* ── conservation identity, pinned and scrubbed ─────────────────────── */

interface Term {
  op: string;
  name: string;
  paise: number;
  kind?: "credit" | "residual";
}

function Identity({ rows }: { rows: ConservationView[] }) {
  const rootRef = useRef<HTMLElement>(null);

  const { terms, balances } = useMemo(() => {
    const sum = (pick: (r: ConservationView) => MoneyView) =>
      rows.reduce((total, row) => total + pick(row).paise, 0);

    const credit = sum((r) => r.credit);
    const t: Term[] = [
      { op: "", name: "settled gross", paise: sum((r) => r.settled_gross) },
      { op: "−", name: "refunds", paise: sum((r) => r.refunds) },
      { op: "−", name: "fees", paise: sum((r) => r.fees) },
      { op: "−", name: "tax", paise: sum((r) => r.tax) },
      { op: "−", name: "chargebacks", paise: sum((r) => r.chargebacks) },
      { op: "−", name: "adjustments", paise: sum((r) => r.adjustments) },
      { op: "+", name: "reversals", paise: sum((r) => r.reversals) },
      { op: "+", name: "unexplained", paise: sum((r) => r.unexplained), kind: "residual" },
    ];
    const reconstructed =
      t[0].paise - t[1].paise - t[2].paise - t[3].paise - t[4].paise - t[5].paise + t[6].paise + t[7].paise;

    return {
      terms: [{ op: "", name: "bank credit", paise: credit, kind: "credit" as const }, ...t],
      balances: reconstructed === credit && rows.every((r) => r.balances),
    };
  }, [rows]);

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      if (REDUCED()) {
        gsap.set(".term", { opacity: 1, y: 0 });
        gsap.set(".identity__bar span", { scaleX: 1 });
        return;
      }

      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: "top top",
          end: "+=180%",
          pin: ".identity__stage",
          scrub: 0.7,
        },
      });

      // Each term resolves as you scroll: the equation assembles itself
      // rather than arriving pre-solved.
      tl.to(".term", {
        opacity: 1,
        y: 0,
        duration: 0.5,
        stagger: 0.5,
        ease: "power2.out",
      }).to(".identity__bar span", { scaleX: 1, duration: 1.2, ease: "power2.inOut" }, "-=0.6");
    }, rootRef);
    return () => ctx.revert();
  }, [terms]);

  return (
    <section className="identity" ref={rootRef}>
      <div className="identity__stage">
        <p className="eyebrow">Invariant 3 — money conservation</p>
        <h2>The identity, asserted for every credit.</h2>
        <p className="lede">
          Summed across all {rows.length} bank credits in this batch. It holds exactly, in integer
          paise, with no tolerance — the engine refuses to finish a run in which it does not.
        </p>

        <div className="identity__eq">
          {terms.map((term, index) => (
            <span
              key={term.name}
              className={`term${term.kind ? ` is-${term.kind}` : ""}`}
              style={{ transform: "translateY(14px)" }}
            >
              {term.op && <span className="term__op">{term.op}</span>}
              <span className="term__name">{term.name}</span>
              <span className="term__val">{fmtPaise(term.paise)}</span>
              {index === 0 && <span className="term__op">=</span>}
            </span>
          ))}
        </div>

        <div className="identity__meter">
          <span>{balances ? "BALANCED" : "VIOLATION"}</span>
          <span className="identity__bar">
            <span />
          </span>
          <span>{rows.length} credits</span>
        </div>

        <p className="identity__note">
          <code className="mono">unexplained</code> is a first-class, reportable bucket — never
          rounded away, never silently absorbed. It is the term that makes the other seven
          trustworthy.
        </p>
      </div>
    </section>
  );
}

/* ── score gauges ───────────────────────────────────────────────────── */

function Scores({ cards }: { cards: ScoreCard[] }) {
  const rootRef = useRef<HTMLElement>(null);

  return (
    <section ref={rootRef}>
      <p className="eyebrow">Headline ratios</p>
      <h2>Three numbers, each checkable.</h2>
      <p className="lede">
        Every gauge is a ratio over the signed report, computed as integer basis points in{" "}
        <code className="mono">reviewer/derive.py</code> and unit-tested there. Each card names the
        two quantities it divided, so you can check the arithmetic rather than trust the arc.
      </p>
      <FinancialScoreCards cards={cards} />
    </section>
  );
}

/* ── settlement volume chart ────────────────────────────────────────── */

function Volume({ points }: { points: TimelinePoint[] }) {
  const rootRef = useRef<HTMLElement>(null);
  const holderRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 380 });

  useEffect(() => {
    const element = holderRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: 380 });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      if (REDUCED()) return;
      gsap.from(holderRef.current, {
        opacity: 0,
        y: 26,
        duration: 0.85,
        ease: "power3.out",
        scrollTrigger: { trigger: rootRef.current, start: "top 78%" },
      });
    }, rootRef);
    return () => ctx.revert();
  }, [points]);

  if (!points.length) return null;

  return (
    <section ref={rootRef}>
      <p className="eyebrow">Settlement month</p>
      <h2>Every credit the gateway paid.</h2>
      <p className="lede">
        {points.length} bank credits by value date. Hover for the exact settled gross, the credit
        id, and any residual that credit could not account for.
      </p>
      <div ref={holderRef} style={{ width: "100%" }}>
        <AreaChart data={points} width={size.width} height={size.height} margin={{ top: 20, right: 0, bottom: 30, left: 0 }} />
      </div>
    </section>
  );
}

/* ── lanes ──────────────────────────────────────────────────────────── */

function Lanes({ batch }: { batch: BatchView }) {
  const rootRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      if (REDUCED()) return;
      gsap.from(".lane", {
        y: 34,
        opacity: 0,
        duration: 0.8,
        ease: "power3.out",
        stagger: 0.11,
        scrollTrigger: { trigger: rootRef.current, start: "top 78%" },
      });
      gsap.utils.toArray<HTMLElement>(".lane__count").forEach((el) => {
        const target = Number(el.dataset.value ?? 0);
        const value = { v: 0 };
        gsap.to(value, {
          v: target,
          duration: 1.1,
          ease: "power2.out",
          scrollTrigger: { trigger: el, start: "top 88%" },
          onUpdate: () => {
            el.textContent = Math.round(value.v).toString();
          },
        });
      });
    }, rootRef);
    return () => ctx.revert();
  }, [batch]);

  return (
    <section ref={rootRef}>
      <p className="eyebrow">Autonomy lanes</p>
      <h2>Three lanes, conformally calibrated.</h2>
      <p className="lede">
        Where a finding lands is decided by calibrated confidence, not by its size. Only one lane
        may post to the ledger without a human.
      </p>

      <div className="lanes">
        {batch.lanes.map((lane) => (
          <article key={lane.lane} className={`lane lane--${lane.lane}`}>
            <div className="lane__head">
              <span className="lane__name">{lane.lane}</span>
              <span className={`lane__posts${lane.posts_automatically ? " is-auto" : ""}`}>
                {lane.posts_automatically ? "posts" : "human"}
              </span>
            </div>
            <div className="lane__count" data-value={lane.finding_count}>
              {lane.finding_count}
            </div>
            <div className="lane__impact">₹{money(lane.impact)}</div>
            <p className="lane__desc">{lane.description}</p>
          </article>
        ))}
      </div>

      <p className="lane__note">
        This reviewer is <strong>read-only</strong>. Approving a <code>PROPOSE</code> finding would
        mean posting a journal entry the calibration explicitly declined to certify —{" "}
        <code>core/ledger.py</code> posts <code>Lane.AUTO</code> only, and no approval or reviewer
        record exists in the engine yet. That is new money-path code, and it belongs in its own
        test-first change rather than behind a button.
      </p>
    </section>
  );
}

/* ── clusters ───────────────────────────────────────────────────────── */

function Clusters({
  clusters,
  onOpen,
}: {
  clusters: ClusterView[];
  onOpen: (id: string) => void;
}) {
  const rootRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      if (REDUCED()) return;
      gsap.from(".cluster", {
        opacity: 0,
        x: -14,
        duration: 0.6,
        ease: "power3.out",
        stagger: 0.05,
        scrollTrigger: { trigger: rootRef.current, start: "top 80%" },
      });
      gsap.utils.toArray<HTMLElement>(".cluster__bar span").forEach((bar) => {
        gsap.to(bar, {
          scaleX: Number(bar.dataset.share ?? 0),
          duration: 1,
          ease: "power2.out",
          scrollTrigger: { trigger: bar, start: "top 92%" },
        });
      });
    }, rootRef);
    return () => ctx.revert();
  }, [clusters]);

  if (!clusters.length) {
    return (
      <section ref={rootRef}>
        <p className="eyebrow">Exception clusters</p>
        <h2>Nothing to dispute.</h2>
        <p className="lede">
          This batch produced no findings. Every credit decomposed cleanly and every deduction
          recomputed to the contracted amount.
        </p>
      </section>
    );
  }

  return (
    <section ref={rootRef}>
      <p className="eyebrow">Exception clusters</p>
      <h2>Ranked by money, not by count.</h2>
      <p className="lede">
        {clusters.length} clusters. Each is one cause with a priced claim behind it — open one for
        the contract clause, the recomputed arithmetic, and every finding it covers.
      </p>

      <div className="clusters">
        {clusters.map((cluster) => (
          <button
            key={cluster.cluster_id}
            className="cluster"
            onClick={() => onOpen(cluster.cluster_id)}
          >
            <span className="cluster__title">
              {cluster.discrepancy_class.replace(/_/g, " ")}
              <span className="cluster__rule">{cluster.rule_id ?? "no rule resolved"}</span>
            </span>
            <span className="cluster__amount">₹{money(cluster.impact)}</span>
            <span className="cluster__meta">
              {cluster.count} finding{cluster.count === 1 ? "" : "s"} ·{" "}
              {(cluster.share_of_unaccounted_bps / 100).toFixed(1)}% of clustered impact
            </span>
            <span className="cluster__bar">
              <span data-share={cluster.share_of_unaccounted_bps / 10000} />
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

/* ── detail drawer ──────────────────────────────────────────────────── */

function Drawer({
  runId,
  clusterId,
  onClose,
}: {
  runId: string;
  clusterId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ClusterDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [explain, setExplain] = useState<{ id: string; text: string } | null>(null);
  const scrimRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    setExplain(null);
    api
      .cluster(runId, clusterId)
      .then((d) => alive && setDetail(d))
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [runId, clusterId]);

  useLayoutEffect(() => {
    const duration = REDUCED() ? 0 : 0.42;
    gsap.to(scrimRef.current, { opacity: 1, duration });
    gsap.to(panelRef.current, { x: 0, duration, ease: "expo.out" });
  }, []);

  const dismiss = useCallback(() => {
    if (REDUCED()) return onClose();
    gsap.to(scrimRef.current, { opacity: 0, duration: 0.28 });
    gsap.to(panelRef.current, { x: "100%", duration: 0.34, ease: "power3.in", onComplete: onClose });
  }, [onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && dismiss();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dismiss]);

  const openExplain = async (recordId: string) => {
    setExplain({ id: recordId, text: "loading…" });
    try {
      const response = await api.explain(runId, recordId);
      setExplain({ id: recordId, text: response.text });
    } catch (e) {
      setExplain({ id: recordId, text: `could not explain ${recordId}: ${(e as Error).message}` });
    }
  };

  return (
    <>
      <div className="scrim" ref={scrimRef} onClick={dismiss} />
      <aside className="drawer" ref={panelRef} role="dialog" aria-modal="true">
        <header className="drawer__head">
          <div>
            <div className="drawer__title">
              {detail ? detail.cluster.discrepancy_class.replace(/_/g, " ") : "Loading"}
            </div>
            <div className="drawer__sub">{clusterId}</div>
          </div>
          <button className="drawer__close" onClick={dismiss} aria-label="Close">
            ESC
          </button>
        </header>

        <div className="drawer__body">
          {error && <p className="err">{error}</p>}
          {!detail && !error && <div className="spinner" />}

          {detail && (
            <>
              <div>
                <div className="block__label">Claim</div>
                <p className="claim">{detail.claim || "—"}</p>
              </div>

              {detail.narration && (
                <div>
                  <div className="block__label">Narration ({detail.narration_status})</div>
                  <p style={{ color: "var(--ink-dim)" }}>{detail.narration}</p>
                </div>
              )}

              {detail.contract_clause && (
                <div>
                  <div className="block__label">Contract clause</div>
                  <blockquote className="clause">{detail.contract_clause}</blockquote>
                </div>
              )}

              {detail.arithmetic && (
                <div>
                  <div className="block__label">Recomputed arithmetic</div>
                  <dl className="arith">
                    <div>
                      <dt>Rule</dt>
                      <dd>{detail.arithmetic.rule_id ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>Rate</dt>
                      <dd>
                        {detail.arithmetic.rate_bps === null
                          ? "—"
                          : `${detail.arithmetic.rate_bps} bps`}
                      </dd>
                    </div>
                    <div>
                      <dt>Fixed fee</dt>
                      <dd>
                        {detail.arithmetic.fixed_fee_paise === null
                          ? "—"
                          : `₹${fmtPaise(detail.arithmetic.fixed_fee_paise)}`}
                      </dd>
                    </div>
                    <div>
                      <dt>Delta</dt>
                      <dd style={{ color: "var(--gold)" }}>
                        ₹{money(detail.arithmetic.representative_delta)}
                      </dd>
                    </div>
                  </dl>
                </div>
              )}

              <div>
                <div className="block__label">
                  Findings ({detail.findings.length}) — click a record to drill down
                </div>
                {detail.findings.map((finding) => (
                  <div key={finding.id} className="finding">
                    <div className="finding__top">
                      <span className="finding__id">{finding.id}</span>
                      <span style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                        <span className={`badge badge--${finding.lane}`}>{finding.lane}</span>
                        <span className="finding__amt">₹{money(finding.impact)}</span>
                      </span>
                    </div>
                    <p className="finding__exp">{finding.explanation}</p>
                    <div className="finding__evidence">
                      {finding.evidence.map((ref) => (
                        <button
                          key={`${ref.type}:${ref.id}`}
                          className="chip"
                          onClick={() => openExplain(ref.id)}
                        >
                          {ref.type} {ref.id}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              {explain && (
                <div>
                  <div className="block__label">assay explain {explain.id}</div>
                  <pre className="explain">{explain.text}</pre>
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
}

/* ── provenance ─────────────────────────────────────────────────────── */

function Provenance({ batch }: { batch: BatchView }) {
  const rootRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const ctx = gsap.context(() => {
      if (REDUCED()) return;
      gsap.from(".prov div", {
        opacity: 0,
        y: 16,
        duration: 0.6,
        stagger: 0.05,
        ease: "power3.out",
        scrollTrigger: { trigger: rootRef.current, start: "top 82%" },
      });
    }, rootRef);
    return () => ctx.revert();
  }, [batch]);

  return (
    <section ref={rootRef}>
      <p className="eyebrow">Provenance</p>
      <h2>The same inputs produce this same page.</h2>
      <p className="lede">
        Every figure above is read from a report that carries the SHA-256 of its own canonical JSON.
        Re-run <code className="mono">assay replay {batch.run_id}</code> to recompute it from
        scratch and compare.
      </p>

      <dl className="prov">
        <div>
          <dt>Report hash</dt>
          <dd className="hash">{batch.report_hash}</dd>
        </div>
        <div>
          <dt>Input hash</dt>
          <dd>{batch.input_hash}</dd>
        </div>
        <div>
          <dt>Contract</dt>
          <dd>{batch.contract_version}</dd>
        </div>
        <div>
          <dt>Rounding</dt>
          <dd>{batch.rounding_policy}</dd>
        </div>
        <div>
          <dt>Git SHA</dt>
          <dd>{batch.git_sha}</dd>
        </div>
        <div>
          <dt>Seed</dt>
          <dd>{batch.seed}</dd>
        </div>
        <div>
          <dt>Merchant</dt>
          <dd>{batch.merchant_id}</dd>
        </div>
        <div>
          <dt>Journal entries posted</dt>
          <dd>{batch.journal_entry_count}</dd>
        </div>
      </dl>
    </section>
  );
}

/* ── app ────────────────────────────────────────────────────────────── */

export default function App() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [batch, setBatch] = useState<BatchView | null>(null);
  const [clusters, setClusters] = useState<ClusterView[]>([]);
  const [conservation, setConservation] = useState<ConservationView[]>([]);
  const [scores, setScores] = useState<ScoreCard[]>([]);
  const [timeline, setTimeline] = useState<TimelinePoint[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openCluster, setOpenCluster] = useState<string | null>(null);
  const railRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .runs()
      .then((rows) => {
        const usable = rows.filter((r) => r.available);
        setRuns(usable);
        if (!usable.length) {
          setError("The audit store has no completed run with a report.json on disk.");
          return;
        }
        setRunId(usable[0].run_id);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setBatch(null);
    Promise.all([
      api.batch(runId),
      api.clusters(runId),
      api.conservation(runId),
      api.scores(runId),
      api.timeline(runId),
    ])
      .then(([b, c, cons, sc, tl]) => {
        if (!alive) return;
        setBatch(b);
        setClusters(c);
        setConservation(cons);
        setScores(sc);
        setTimeline(tl);
      })
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [runId]);

  useLayoutEffect(() => {
    if (!batch) return;
    const ctx = gsap.context(() => {
      gsap.to(railRef.current, {
        scaleX: 1,
        ease: "none",
        scrollTrigger: { start: 0, end: "max", scrub: 0.3 },
      });
    });
    // Content height changes as sections mount; ScrollTrigger caches page
    // extents at creation, so a refresh here keeps the pinned identity
    // section aligned with the real document.
    ScrollTrigger.refresh();
    return () => ctx.revert();
  }, [batch, clusters, conservation, scores, timeline]);

  if (error && !batch) return <Failure message={error} />;
  if (!batch) return <Loading />;

  return (
    <>
      <div className="aurora" />
      <div className="grain" />
      <div className="rail">
        <div className="rail__fill" ref={railRef} />
      </div>

      <header className="topbar">
        <div className="brand">
          <span className="brand__mark">Assay</span>
          <span className="brand__sub">Reviewer · read-only</span>
        </div>
        <div className="runpick">
          <select
            value={runId ?? ""}
            onChange={(e) => setRunId(e.target.value)}
            aria-label="Audit run"
          >
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.run_id} · {run.contract_version}
              </option>
            ))}
          </select>
        </div>
      </header>

      <main>
        {/* The scroll gate. Its media is decorative and says nothing about
         * this run -- swap public/hero.svg for any image. onExpanded hands
         * scroll back to ScrollTrigger and forces a refresh, so the pinned
         * identity section measures against the full document height rather
         * than the collapsed one. */}
        <ScrollExpandMedia
          mediaType="image"
          mediaSrc="/hero.svg"
          title="Assay Reviewer"
          date={batch.run_id}
          scrollToExpand="Scroll to open the report"
          onExpanded={() => ScrollTrigger.refresh()}
        >
          <div className="max-w-3xl mx-auto text-center">
            <p style={{ color: "var(--ink-dim)" }}>
              Everything below is read from a report the engine already computed, signed and
              hashed. This page adds no arithmetic of its own.
            </p>
          </div>
        </ScrollExpandMedia>

        <Hero batch={batch} />
        {conservation.length > 0 && <Identity rows={conservation} />}
        {scores.length > 0 && <Scores cards={scores} />}
        <Volume points={timeline} />
        <Lanes batch={batch} />
        <Clusters clusters={clusters} onOpen={setOpenCluster} />
        <Provenance batch={batch} />
      </main>

      <footer>
        Read-only lens over <span className="mono">{batch.run_dir}</span>. This page computes no
        money — every figure is read from the report the engine signed.
      </footer>

      {openCluster && runId && (
        <Drawer runId={runId} clusterId={openCluster} onClose={() => setOpenCluster(null)} />
      )}
    </>
  );
}
