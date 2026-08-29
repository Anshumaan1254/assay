/* The end of the page: how to run it, and the way through to the reviewer.
 *
 * The reviewer is deployed separately as a Vercel Function, so this links
 * straight at the running screen. Nothing in `reviewer/` changed for either
 * this page or that deployment to exist.
 */

import { useLayoutEffect, useRef } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

import { REPO, REVIEWER, audit, count } from "@/lib/audit";
import { prefersReducedMotion } from "@/lib/env";
import ErosionField from "@/components/ErosionField";

gsap.registerPlugin(ScrollTrigger);

const FACTS = [
  { label: "Bank credits decomposed", value: `${audit.decomposition.resolved} / ${audit.decomposition.credits}` },
  { label: "Records audited", value: count(audit.shards.records) },
  { label: "Findings", value: `${audit.findings.count} in ${audit.findings.clusters} clusters` },
  { label: "Unaccounted", value: audit.unaccounted.total.display, accent: true },
];

export default function Closing() {
  const rootRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    if (prefersReducedMotion()) return;
    const context = gsap.context(() => {
      gsap.from(".closing__reveal", {
        y: 26,
        opacity: 0,
        duration: 0.8,
        ease: "power3.out",
        stagger: 0.08,
        scrollTrigger: { trigger: rootRef.current, start: "top 75%", once: true },
      });
    }, rootRef);
    return () => context.revert();
  }, []);

  return (
    <section className="closing" ref={rootRef} id="run">
      <ErosionField className="closing__field" />

      <p className="closing__eyebrow closing__reveal">Run it yourself</p>
      <h2 className="closing__title closing__reveal">Thirty seconds. No API key.</h2>

      <pre className="closing__terminal closing__reveal">
        <code>
          <span className="closing__prompt">$</span> git clone {REPO.replace("https://", "")}
          {"\n"}
          <span className="closing__prompt">$</span> make demo
        </code>
      </pre>

      <dl className="closing__facts closing__reveal">
        {FACTS.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd className={`num${fact.accent ? " is-accent" : ""}`}>{fact.value}</dd>
          </div>
        ))}
      </dl>

      <div className="closing__cards">
        <a className="closing__card closing__card--accent closing__reveal" href={REVIEWER}>
          <p className="closing__card-label">The reviewer</p>
          <h3>See a single finding, all the way down.</h3>
          <p className="closing__card-body">
            Read-only lens over a computed audit — the conservation identity assembling term by
            term, per-run scorecards, drill-down to one transaction. No write routes exist, and a
            test asserts it.
          </p>
          <span className="closing__go">assay-7o7a.vercel.app →</span>
        </a>

        <a className="closing__card closing__reveal" href={REPO}>
          <p className="closing__card-label">The source</p>
          <h3>The CLI is the product.</h3>
          <p className="closing__card-body">
            generate · contract compile · audit · eval · replay · explain · chaos · fetch.
            Python 3.11, integer paise, no pandas in the engine.
          </p>
          <span className="closing__go">github.com/Anshumaan1254/assay →</span>
        </a>

        <a className="closing__card closing__reveal" href={`${REPO}/blob/main/EVIDENCE.md`}>
          <p className="closing__card-label">The evidence</p>
          <h3>Every number on this page, and the ones that hurt.</h3>
          <p className="closing__card-body">
            Per-class precision and recall, the confusion matrix, calibration, the conformal
            guarantee — and what the engine still misses entirely.
          </p>
          <span className="closing__go">EVIDENCE.md →</span>
        </a>
      </div>

      <footer className="closing__foot closing__reveal">
        <p>
          Every figure is read from one signed report. This page performs no arithmetic on money.
        </p>
        <p className="num closing__hash">
          {audit.provenance.run_id} · {audit.provenance.contract_version} ·{" "}
          {audit.provenance.report_hash}
        </p>
      </footer>
    </section>
  );
}
