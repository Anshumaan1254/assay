/* The erosion field, mounted where a section actually wants it.
 *
 * Used by more than one section, so it is wrapped once here rather than
 * copied. The wrapper does two things the raw component does not:
 *
 *   - hides itself with `display:none` when the section is nowhere near the
 *     viewport. The field is an iframe running its own requestAnimationFrame
 *     loop, and browsers throttle rAF hard inside a display:none frame, so a
 *     second instance further down the page costs nothing while you are
 *     reading the top of it. Hidden rather than unmounted, because
 *     unmounting drops the iframe and remounting reloads it -- a visible
 *     flash, for no gain.
 *
 *   - skips the whole thing under prefers-reduced-motion: the field is
 *     continuous ambient movement and there is no static frame of it worth
 *     showing.
 */

import { lazy, Suspense, useEffect, useRef, useState } from "react";

import { prefersReducedMotion } from "@/lib/env";

const RecursiveErosionBackground = lazy(() => import("@/components/ui/recursive-erosion"));

export default function ErosionField({ className = "" }: { className?: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  const [reduced] = useState(prefersReducedMotion);

  useEffect(() => {
    if (reduced) return;
    const host = hostRef.current;
    if (!host) return;

    // A generous margin: start it a full viewport early so it is already
    // running by the time any of it is on screen.
    const observer = new IntersectionObserver(
      ([entry]) => setNear(entry.isIntersecting),
      { rootMargin: "100% 0px 100% 0px" },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, [reduced]);

  if (reduced) return null;

  return (
    <div ref={hostRef} className={`erosion ${className}`.trim()} aria-hidden="true">
      <div className="erosion__inner" hidden={!near}>
        <Suspense fallback={null}>
          <RecursiveErosionBackground mode="dark" />
        </Suspense>
      </div>
    </div>
  );
}
