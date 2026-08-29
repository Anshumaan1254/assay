/* The argument, one line at a time.
 *
 * A pinned stage with the erosion field centred behind it and the lines
 * placed around it — each one lands somewhere different, so the eye moves
 * across the frame instead of reading a slideshow in a fixed box.
 *
 * The tweens deliberately overlap: a line begins clearing while the next is
 * already fading up, so the two cross rather than one blinking out and the
 * other blinking in. Everything is a function of scroll position; nothing
 * here loops.
 *
 * Figures come from `lib/audit.ts`, which is baked from a signed report.
 * This file writes no number of its own.
 */

import { useLayoutEffect, useRef, useState } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

import { audit, count } from "@/lib/audit";
import { acquireSmoothScroll } from "@/lib/smooth-scroll";
import { prefersReducedMotion } from "@/lib/env";
import ErosionField from "@/components/ErosionField";

gsap.registerPlugin(ScrollTrigger);

type Place = "tl" | "tr" | "bl" | "br" | "l" | "r" | "c";

interface Line {
  eyebrow?: string;
  text: string;
  figure?: string;
  accent?: boolean;
  /** Where on the stage this line lands. No two adjacent lines share one,
   *  so each arrival reads as a move rather than a redraw. */
  place: Place;
}

const LINES: Line[] = [
  {
    eyebrow: "The problem",
    text: "A gateway deducts, then tells you what it deducted.",
    place: "l",
  },
  {
    text: "Nobody outside the gateway ever recomputes that number.",
    figure: `${audit.gap.display} kept this month — ${audit.gap.share_pct}% of volume`,
    place: "tr",
  },
  {
    eyebrow: "What it does",
    text: "Assay rebuilds every bank credit from the payments that made it.",
    figure: `${audit.credits} credits · ${count(audit.shards.total)} payments · ${audit.decomposition.structural} joined, ${audit.decomposition.subset_sum} solved`,
    place: "bl",
  },
  {
    text: "Then reprices every deduction against your own rate card.",
    figure: "Tiered MDR · flat fees · caps · GST · mid-month revisions",
    place: "r",
  },
  {
    eyebrow: "The rule",
    text: "No language model ever computes an amount.",
    figure: `${count(audit.evidence.records_touching_a_model)} of ${count(audit.evidence.records_total)} records ever touched one`,
    place: "tl",
  },
  {
    text: "Same inputs, same bytes — every report signs its own hash.",
    figure: `replay matched · ${audit.evidence.replay_live_calls} live calls`,
    place: "br",
  },
  {
    eyebrow: "The evidence",
    text: "It claimed nothing that was not there.",
    figure: `${audit.evidence.falsely_claimed.display} falsely claimed across ${audit.evidence.months} months`,
    accent: true,
    place: "l",
  },
  {
    text: "The calibration refused to certify autonomy, so nothing auto-posts.",
    figure: `all ${count(audit.evidence.findings_total)} findings routed to a human`,
    place: "tr",
  },
  {
    eyebrow: "The residual",
    text: "Three payments belonged to no credit at all. It named them.",
    figure: `${audit.unaccounted.unclaimed.display} — ${audit.unaccounted.unclaimed_ids.join("  ")}`,
    accent: true,
    place: "bl",
  },
];

export default function LineStory() {
  const rootRef = useRef<HTMLElement>(null);
  const pinRef = useRef<HTMLDivElement>(null);
  const [reduced] = useState(prefersReducedMotion);

  useLayoutEffect(() => {
    if (reduced) return;
    const release = acquireSmoothScroll();

    const context = gsap.context(() => {
      const lines = gsap.utils.toArray<HTMLElement>(".linestory__line");
      gsap.set(lines, { autoAlpha: 0, y: 40, filter: "blur(14px)" });

      const timeline = gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: "top top",
          // Roughly one viewport of scroll per line, plus room for the last
          // one to land before the section hands over.
          end: `+=${LINES.length * 95 + 40}%`,
          scrub: 0.9,
          pin: pinRef.current,
          anticipatePin: 1,
        },
      });

      const SLOT = 2;
      lines.forEach((line, index) => {
        const at = index * SLOT;
        timeline.to(
          line,
          { autoAlpha: 1, y: 0, filter: "blur(0px)", duration: 0.95, ease: "power2.out" },
          at,
        );
        // Starts clearing at +1.35 and runs 1.1, so it is still fading when
        // the next line begins at +2.0 — the two genuinely cross. The last
        // line stays up and hands the reader to the section below rather
        // than leaving a blank pinned stage.
        if (index < lines.length - 1) {
          timeline.to(
            line,
            { autoAlpha: 0, y: -40, filter: "blur(14px)", duration: 1.1, ease: "power2.in" },
            at + 1.35,
          );
        }
      });
    }, rootRef);

    return () => {
      context.revert();
      release();
    };
  }, [reduced]);

  return (
    <section className={`linestory${reduced ? " is-static" : ""}`} ref={rootRef}>
      <div className="linestory__pin" ref={pinRef}>
        {/* The erosion field, centred, behind the lines. Scoped to this
            section rather than fixed behind the whole page: it belongs to
            this beat, and a section that paints its own opaque ground (the
            clip-path reveal below) would otherwise just cover it. */}
        <ErosionField className="linestory__field" />

        <div className="linestory__stage">
          {LINES.map((line) => (
            <div className={`linestory__line is-${line.place}`} key={line.text}>
              {line.eyebrow && <p className="linestory__eyebrow">{line.eyebrow}</p>}
              <p className="linestory__text">{line.text}</p>
              {line.figure && (
                <p className={`linestory__figure num${line.accent ? " is-accent" : ""}`}>
                  {line.figure}
                </p>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
